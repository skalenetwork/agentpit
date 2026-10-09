"""Seed the 2026 FIFA World Cup Winner event with seven sub-markets.

Walks the live API to register a temporary admin user, create one binary
market per country (each with on-chain prepareCondition + registerToken via
the existing local-creation path), then writes the parent event and binds
every market to it via the SQLite DAL.

Usage:
    python scripts/seed_world_cup_event.py
    python scripts/seed_world_cup_event.py --base http://localhost:8000

Re-running is safe: countries already attached to the event are skipped.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Add repo root so the script can import the package without an install step.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agentpit.config import Settings  # noqa: E402
from agentpit.db.session import DbSession  # noqa: E402
from agentpit.db.table_read import TableRead  # noqa: E402
from agentpit.db.table_write import TableWrite  # noqa: E402


EVENT_SLUG = "2026-fifa-world-cup-winner"
EVENT_TITLE = "2026 FIFA World Cup Winner"
EVENT_DESCRIPTION = (
    "Who will lift the 2026 FIFA World Cup? "
    "Each country resolves YES if they win the tournament."
)
EVENT_ICON = (
    "https://upload.wikimedia.org/wikipedia/en/0/04/2026_FIFA_World_Cup_emblem.svg"
)
EVENT_CATEGORY = "Sports"
# Approx Jul 19, 2026 — the scheduled final.
EVENT_END_DATE = int(time.mktime(time.strptime("2026-07-19", "%Y-%m-%d")))


@dataclass(frozen=True)
class Country:
    name: str
    flag: str  # emoji used as the icon — UI may swap for a flag SVG later


COUNTRIES: tuple[Country, ...] = (
    Country("France", "🇫🇷"),
    Country("Spain", "🇪🇸"),
    Country("England", "🏴󠁧󠁢󠁥󠁮󠁧󠁿"),
    Country("Brazil", "🇧🇷"),
    Country("Argentina", "🇦🇷"),
    Country("Portugal", "🇵🇹"),
    Country("Germany", "🇩🇪"),
)


def _request(
    base: str,
    path: str,
    *,
    method: str = "GET",
    body: dict | None = None,
    token: str | None = None,
    admin_token: str | None = None,
) -> dict[str, Any]:
    url = f"{base.rstrip('/')}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    if admin_token is not None:
        req.add_header("X-Admin-Token", admin_token)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        raise RuntimeError(f"{method} {path} → HTTP {e.code}: {body_text}") from None


def register_admin(base: str) -> tuple[str, str]:
    stamp = int(time.time())
    email = f"seed-wc-admin-{stamp}@example.com"
    res = _request(
        base,
        "/register",
        method="POST",
        body={"email": email, "password": "seedpass-12345"},
    )
    return res["access_token"], res["user"]["eth_address"]


def create_country_market(
    base: str,
    token: str,
    country: Country,
    *,
    end_date: int,
    admin_token: str | None = None,
) -> dict[str, Any]:
    payload = {
        "question": f"Will {country.name} win the 2026 FIFA World Cup?",
        "description": (
            f"Resolves YES if {country.name} wins the 2026 FIFA World Cup. "
            "Otherwise resolves NO."
        ),
        "outcome_labels": ["Yes", "No"],
        "slug": f"will-{country.name.lower()}-win-2026-fifa-world-cup",
        "end_date": end_date,
        "state": "ACTIVE",
        "outcome_label": country.name,
        "icon_url": country.flag,
    }
    return _request(
        base,
        "/markets",
        method="POST",
        body=payload,
        token=token,
        admin_token=admin_token,
    )


def attach_markets_to_event(market_ids: list[int]) -> int:
    settings = Settings()
    session = DbSession(settings.db_path)
    attached = 0
    try:
        with session.write() as conn:
            event = TableWrite.upsert_event(
                conn,
                slug=EVENT_SLUG,
                title=EVENT_TITLE,
                description=EVENT_DESCRIPTION,
                icon_url=EVENT_ICON,
                category=EVENT_CATEGORY,
                end_date=EVENT_END_DATE,
            )
            for mid in market_ids:
                market = TableRead.read_market(conn, mid)
                if market is None:
                    print(f"   ! market {mid} not found, skipping", file=sys.stderr)
                    continue
                TableWrite.attach_market_to_event(
                    conn,
                    market_id=mid,
                    event_id=event.event_id,
                    outcome_label=market.outcome_label,
                    icon_url=market.icon_url,
                )
                attached += 1
    finally:
        session.close()
    return attached


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    args = ap.parse_args()

    print(f"→ registering temporary admin user against {args.base}")
    try:
        token, address = register_admin(args.base)
    except (RuntimeError, urllib.error.URLError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"   admin {address}")

    admin_token = Settings().admin_token

    print(
        f"\n→ creating {len(COUNTRIES)} sub-markets (on-chain prepareCondition + registerToken)"
    )
    market_ids: list[int] = []
    for country in COUNTRIES:
        try:
            market = create_country_market(
                args.base,
                token,
                country,
                end_date=EVENT_END_DATE,
                admin_token=admin_token,
            )
        except RuntimeError as exc:
            print(f"   ✗ {country.name:<10} skipped — {exc}", file=sys.stderr)
            continue
        mid = int(market["market_id"])
        market_ids.append(mid)
        print(f"   ✓ {country.name:<10} market_id={mid}")

    if not market_ids:
        print("\nERROR: no markets created — aborting", file=sys.stderr)
        return 1

    print(f"\n→ binding {len(market_ids)} market(s) to event '{EVENT_SLUG}'")
    attached = attach_markets_to_event(market_ids)
    print(f"   attached {attached}/{len(market_ids)}")

    print(f"\n✓ done — open {args.base.replace(':8000', ':5173')}/events/{EVENT_SLUG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
