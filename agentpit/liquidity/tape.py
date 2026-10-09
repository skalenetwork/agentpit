"""Mirror the real Polymarket tape: one synthetic trades row per
last_trade_price WSS event.

STATUS='MIRRORED' is distinct for provenance yet passes every reader's
`STATUS != 'FAILED'` filter (last-trade-price, price history, charts).
TAKER/MAKER_API_KEY are fabricated constants so user-scoped feeds
(/data/trades, /activity) never surface these rows. No FK constraints exist
on trades (table_create.py), so order-less rows are safe.

MATCH_KIND is always 'NORMAL'. ASSET_ID is the local YES token and
MAKER_ASSET_ID the local NO token, from which `TableRead.TOKEN_PRINTS_CTE`
derives the NO print at 1 - p.
"""

import secrets

import psycopg

MIRROR_TRADE_STATUS = "MIRRORED"
MIRROR_API_KEY = "mirror-tape"  # opaque, never a real user's api key

MirroredPrint = tuple[str, str, str, int, int, str, int]


def insert_mirrored_trades(
    conn: psycopg.Connection,
    prints: list[MirroredPrint],
) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO trades (
                TRADE_ID, TAKER_ORDER_ID, MAKER_ORDERS, MARKET, ASSET_ID,
                MAKER_ASSET_ID, MATCH_KIND,
                PRICE, TRADE_SIZE, REMAINING_SIZE, SIDE, STATUS,
                MATCH_TIME, TRANSACTION_HASH, BUCKET_INDEX, FEE_RATE_BPS,
                TAKER_API_KEY, MAKER_API_KEY
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    f"mirror-{secrets.token_hex(12)}",
                    "",
                    "[]",
                    condition_id,
                    yes_token,
                    no_token,
                    "NORMAL",
                    price_micro,
                    size_micro,
                    0,
                    side,
                    MIRROR_TRADE_STATUS,
                    match_time_s,
                    "",
                    0,
                    0,
                    MIRROR_API_KEY,
                    MIRROR_API_KEY,
                )
                for condition_id, yes_token, no_token, price_micro, size_micro, side, match_time_s in prints
            ],
        )
