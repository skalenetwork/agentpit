"""What one account actually did on one trade row.

`trades.PRICE` is always the MAKER's price and `trades.ASSET_ID` is always the
TAKER's token. That is the whole truth for a NORMAL match, where both parties
transact in the same token at the same price. It is not for the two special
kinds:

| kind   | the taker's leg                  | the maker's leg                     |
| ------ | --------------------------------- | ------------------------------------ |
| NORMAL | ASSET_ID, side SIDE, price p      | ASSET_ID, opposite of SIDE, price p  |
| MINT   | ASSET_ID, BUY, price MICRO - p    | MAKER_ASSET_ID, BUY, price p         |
| MERGE  | ASSET_ID, SELL, price MICRO - p   | MAKER_ASSET_ID, SELL, price p        |

A MINT mints a complementary pair for $1, so both sides ACQUIRE, of different
tokens, at prices summing to MICRO. A MERGE burns one, so both sides DISPOSE.

This is the per-user view: a NORMAL match yields TWO legs, the buyer's and the
seller's. Do not confuse it with a price print, where a NORMAL match yields
exactly ONE — see `TOKEN_PRINTS_CTE` in `agentpit/db/table_read.py`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

#: Prices and sizes are integers scaled by 10**6; MICRO is $1.00.
MICRO = 1_000_000


@dataclass(frozen=True)
class Leg:
    token_id: str
    side: Literal["BUY", "SELL"]
    price_micro: int
    size_micro: int
    is_taker: bool


def _leg(row: Mapping[str, Any], *, is_taker: bool) -> Leg:
    kind = row["MATCH_KIND"] or "NORMAL"
    price = int(row["PRICE"])          # always the MAKER's price
    asset = row["ASSET_ID"]
    maker_asset = row["MAKER_ASSET_ID"] or asset
    if kind == "MINT":
        side = "BUY"
    elif kind == "MERGE":
        side = "SELL"
    else:
        # One token, opposite directions, one price.
        side = row["SIDE"] if is_taker else (
            "SELL" if row["SIDE"] == "BUY" else "BUY"
        )
    if is_taker:
        token = asset
        # The pair costs $1: the maker put up `price`, so the taker put up the
        # rest. NORMAL keeps the single price both parties agreed on.
        if kind in ("MINT", "MERGE"):
            price = MICRO - price
    else:
        token = asset if kind == "NORMAL" else maker_asset
    return Leg(
        token_id=token,
        side=side,
        price_micro=price,
        size_micro=int(row["TRADE_SIZE"]),
        is_taker=is_taker,
    )


def legs_for_user(row: Mapping[str, Any], api_key: str) -> list[Leg]:
    """Every leg of `row` that `api_key` holds, newest-first order irrelevant.

    Usually one. Two when the account is on BOTH sides: the matcher has no
    same-account guard, so a resting order of theirs can be crossed by their
    own later order, and both legs are real.
    """
    legs: list[Leg] = []
    if row["TAKER_API_KEY"] == api_key:
        legs.append(_leg(row, is_taker=True))
    if row["MAKER_API_KEY"] == api_key:
        legs.append(_leg(row, is_taker=False))
    return legs
