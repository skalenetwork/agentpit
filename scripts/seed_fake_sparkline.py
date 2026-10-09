"""Seed fake trade rows for sparkline rendering.

Inserts a synthetic mean-reverting price walk into the `trades` table so
the home-page card + event-detail chart have something to render before
the live trade flow has accumulated history. The data is bogus and
should only be used against a dev DB.

Usage:
    python scripts/seed_fake_sparkline.py --slug xi-jinping-out-before-2027
    python scripts/seed_fake_sparkline.py --slug … --days 30 --mean-cents 12

Defaults:
    --days: 30
    --mean-cents: the live order-book mid (the value the UI legend shows) if
                  the book has resting orders, else the most recent trade,
                  else 50. The final sample is pinned to this value so the
                  chart's end dot lines up with the legend.
    --points-per-day: 24 (one point/hour)
    --db: $AGENTPIT_DB_PATH (or "agentpit.db" in cwd)
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
from datetime import datetime, timezone


_PRICE_MICRO_UNIT = 1_000_000  # micro-USDC per $1.00 of price
_PRICE_MIN = 1_000   # avoid sticky-at-zero walks
_PRICE_MAX = 999_000


def _resolve_yes_token(conn: sqlite3.Connection, slug: str) -> tuple[int, str, str]:
    """Find the market for either an event slug or a market slug.

    Returns (market_id, yes_token_id, market_question). When `slug` matches
    an event with multiple markets, we error out — fake price walks per
    market would be misleading.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT m.MARKET_ID, m.ERC1155_TOKENS, m.QUESTION "
        "FROM markets m "
        "LEFT JOIN events e ON e.EVENT_ID = m.EVENT_ID "
        "WHERE m.SLUG = ? OR e.SLUG = ?",
        (slug, slug),
    ).fetchall()
    if not rows:
        sys.exit(f"no market or event found for slug {slug!r}")
    if len(rows) > 1:
        sys.exit(
            f"slug {slug!r} resolved to {len(rows)} markets; pass a market slug "
            "to disambiguate"
        )
    row = rows[0]
    tokens = json.loads(row["ERC1155_TOKENS"]) if row["ERC1155_TOKENS"] else []
    if not tokens or not tokens[0]:
        sys.exit(f"market {row['MARKET_ID']} has no ERC1155 tokens")
    return int(row["MARKET_ID"]), str(tokens[0][0]), str(row["QUESTION"])


def _orderbook_mid_micro(conn: sqlite3.Connection, yes_token: str) -> int | None:
    """Mid-price (micro-USDC) of the live YES book — the same number the UI
    legend renders. Mirrors the frontend `computeMid`: the midpoint of the
    best bid and best ask, falling back to whichever side exists.
    """
    bid = conn.execute(
        "SELECT MAX(PRICE) FROM orders "
        "WHERE TOKEN_ID = ? AND SIDE = 'BUY' AND STATUS = 'live'",
        (yes_token,),
    ).fetchone()[0]
    ask = conn.execute(
        "SELECT MIN(PRICE) FROM orders "
        "WHERE TOKEN_ID = ? AND SIDE = 'SELL' AND STATUS = 'live'",
        (yes_token,),
    ).fetchone()[0]
    if bid is not None and ask is not None:
        return (int(bid) + int(ask)) // 2
    if ask is not None:
        return int(ask)
    if bid is not None:
        return int(bid)
    return None


def _generate_walk(
    *,
    points: int,
    start_t: int,
    interval_s: int,
    mean_micro: int,
    vol_micro: int,
) -> list[tuple[int, int]]:
    """Build (timestamp, price_micro_usd) pairs via a mean-reverting walk.

    The walk pulls toward `mean_micro` proportional to current deviation
    so prices don't drift away over long windows. Each step also takes a
    fresh gaussian shock of size `vol_micro`.
    """
    out: list[tuple[int, int]] = []
    price = mean_micro
    for i in range(points):
        t = start_t + i * interval_s
        # Mean reversion: pull 15% of the gap toward the mean per step.
        price += int((mean_micro - price) * 0.15)
        # Random shock.
        price += int(random.gauss(0, vol_micro))
        price = max(_PRICE_MIN, min(_PRICE_MAX, price))
        out.append((t, price))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug", required=True, help="event or market slug")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--mean-cents", type=int, default=None)
    parser.add_argument("--vol-cents", type=int, default=2)
    parser.add_argument("--points-per-day", type=int, default=24)
    parser.add_argument(
        "--db",
        default=os.environ.get("AGENTPIT_DB_PATH", "agentpit.db"),
    )
    parser.add_argument(
        "--seed", type=int, default=42, help="random seed for reproducibility"
    )
    args = parser.parse_args()

    if args.db == ":memory:":
        sys.exit("can't seed an in-memory DB — set --db or AGENTPIT_DB_PATH")
    if not os.path.exists(args.db):
        sys.exit(f"db file {args.db!r} does not exist")

    random.seed(args.seed)

    conn = sqlite3.connect(args.db)
    try:
        market_id, yes_token, question = _resolve_yes_token(conn, args.slug)
        if args.mean_cents is not None:
            mean_micro = args.mean_cents * 10_000
        else:
            # Anchor to the live order-book mid so the walk — and crucially its
            # final point — line up with the price the UI legend shows (the
            # legend reads the order book; this chart reads `trades`). Fall back
            # to the most recent trade, then a coin-flip 50¢.
            mean_micro = _orderbook_mid_micro(conn, yes_token)
            if mean_micro is None:
                row = conn.execute(
                    "SELECT PRICE FROM trades WHERE ASSET_ID = ? "
                    "ORDER BY MATCH_TIME DESC LIMIT 1",
                    (yes_token,),
                ).fetchone()
                mean_micro = int(row[0]) if row is not None else 500_000

        vol_micro = args.vol_cents * 10_000
        total_points = args.days * args.points_per_day
        interval_s = (args.days * 86_400) // max(1, total_points)
        now = int(datetime.now(timezone.utc).timestamp())
        start_t = now - args.days * 86_400

        walk = _generate_walk(
            points=total_points,
            start_t=start_t,
            interval_s=interval_s,
            mean_micro=mean_micro,
            vol_micro=vol_micro,
        )
        # Pin the final sample exactly to the mid so the chart's end dot equals
        # the legend's current price — otherwise the last random shock leaves
        # them visibly out of sync (and can even flip the ranking).
        if walk:
            anchor = max(_PRICE_MIN, min(_PRICE_MAX, mean_micro))
            walk[-1] = (walk[-1][0], anchor)

        rows = [
            (
                f"fake-{yes_token}-{t}",
                "",                         # TAKER_ORDER_ID
                "[]",                       # MAKER_ORDERS
                yes_token,                  # MARKET (denormalized — schema uses token)
                yes_token,                  # ASSET_ID
                p,                          # PRICE (micro-USDC)
                random.randint(1, 100) * _PRICE_MICRO_UNIT,  # TRADE_SIZE (micro-shares)
                0,                          # REMAINING_SIZE
                "BUY",                      # SIDE
                "CONFIRMED",                # STATUS
                t,                          # MATCH_TIME
                "",                         # TRANSACTION_HASH
                0,                          # BUCKET_INDEX
                0,                          # FEE_RATE_BPS
            )
            for (t, p) in walk
        ]

        # Idempotent re-seed: drop any prior synthetic rows for this token so
        # repeated runs replace the walk instead of stacking duplicates.
        conn.execute(
            "DELETE FROM trades WHERE ASSET_ID = ? AND TRADE_ID LIKE 'fake-%'",
            (yes_token,),
        )

        conn.executemany(
            "INSERT INTO trades ("
            "  TRADE_ID, TAKER_ORDER_ID, MAKER_ORDERS, MARKET, ASSET_ID, "
            "  PRICE, TRADE_SIZE, REMAINING_SIZE, SIDE, STATUS, "
            "  MATCH_TIME, TRANSACTION_HASH, BUCKET_INDEX, FEE_RATE_BPS"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()

        print(
            f"Seeded {len(rows)} fake trades for market {market_id} "
            f"({question!r}) over {args.days} days "
            f"ending at {mean_micro / 10_000:.2f}¢ ± {args.vol_cents}¢ per step."
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
