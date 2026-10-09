"""Derive a market's display prices from Polymarket's book + the trade tape.

agentpit stores order/trade prices as integers scaled by 10**6 (10**6 == $1.00).
This module stays in that integer domain and leaves the conversion to wire
floats/strings to the Gamma serializer, so rounding happens in exactly one place.
"""

from dataclasses import dataclass

from agentpit.datastructures.market import Market
from agentpit.db.table_read import TableRead
from agentpit.liquidity import feed

PRICE_ONE = 10**6  # scaled-int price that equals $1.00 (certainty)


@dataclass(frozen=True)
class MarketPrices:
    """Book/trade-derived prices for one market, as scaled price ints.

    `best_bid` / `best_ask` / `last_trade` describe outcome[0] (the YES / "Up"
    side), matching Gamma's scalar fields. `outcome_prices` carries one price
    per outcome, aligned to `market.erc1155_tokens`.
    """

    best_bid: int | None
    best_ask: int | None
    last_trade: int | None
    outcome_prices: list[int]


def _own_price(bid: int | None, ask: int | None, last: int | None) -> int | None:
    """An outcome's own price: book mid if two-sided, else the single resting
    side, else the last trade, else unknown (None)."""
    if bid is not None and ask is not None:
        return (bid + ask) // 2
    if bid is not None:
        return bid
    if ask is not None:
        return ask
    return last


def compute_market_prices(
    market: Market,
    tops: "dict[str, tuple[int | None, int | None]]",
    lasts: "dict[str, int]",
) -> MarketPrices:
    """Build a MarketPrices for `market` from batched book tops + last trades.

    `tops` maps token id -> (best_bid, best_ask); `lasts` maps token id -> last
    trade price (both scaled ints). Missing entries mean no data. Per-outcome
    price resolution, in priority order:
      1. the outcome's own book mid (or single resting side),
      2. the outcome's own last trade,
      3. for a binary market, the complement (1 - the other outcome's price),
      4. 0.5 as a neutral fallback.
    """
    tokens = [tid for tid, _ in market.erc1155_tokens]
    own: list[tuple[int | None, int | None, int | None, int | None]] = []
    for tid in tokens:
        bid, ask = tops.get(tid, (None, None))
        last = lasts.get(tid)
        own.append((bid, ask, last, _own_price(bid, ask, last)))

    n = len(tokens)
    outcome_prices: list[int] = []
    for i in range(n):
        price = own[i][3]
        if price is None and n == 2:
            other = own[1 - i][3]
            if other is not None:
                price = PRICE_ONE - other
        if price is None:
            price = PRICE_ONE // 2
        outcome_prices.append(max(0, min(PRICE_ONE, price)))

    bid0, ask0, last0 = (
        (own[0][0], own[0][1], own[0][2]) if own else (None, None, None)
    )
    return MarketPrices(
        best_bid=bid0,
        best_ask=ask0,
        last_trade=last0,
        outcome_prices=outcome_prices,
    )


def prices_for_markets(conn, markets: list[Market]) -> "dict[int, MarketPrices]":
    """Batch-derive MarketPrices for many markets in one DB round-trip.

    Collects every outcome token across `markets`, reads the book tops from
    memory and the last trades for all of them at once, then composes
    per-market prices. Keyed by ``market_id``. Empty markets list -> empty
    dict (no queries).
    """
    if not markets:
        return {}
    token_ids: list[str] = []
    for m in markets:
        token_ids.extend(tid for tid, _ in m.erc1155_tokens)
    tops = feed.tops(token_ids)
    lasts = TableRead.last_trade_prices_for_tokens(conn, token_ids)
    return {m.market_id: compute_market_prices(m, tops, lasts) for m in markets}
