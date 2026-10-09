"""A price print is "this token traded at this price", from Polymarket's tape
only. A tape row is a YES print that names the NO token, which prints at
MICRO - p on the opposite side. Agent fills never print.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from agentpit.datastructures.match_leg import MICRO
from agentpit.db.table_read import TableRead
from tests.db_helpers import fresh_test_conn


@pytest.fixture()
def db() -> Any:
    conn = fresh_test_conn()
    yield conn
    conn.close()


def _trade(db, *, asset, maker_asset, kind, side, price, status, t=1000):
    db.execute(
        "INSERT INTO trades (TRADE_ID, ASSET_ID, MAKER_ASSET_ID, MATCH_KIND, "
        "SIDE, PRICE, TRADE_SIZE, STATUS, MATCH_TIME, TAKER_API_KEY, "
        "MAKER_API_KEY) VALUES (%s,%s,%s,%s,%s,%s,100,%s,%s,'tk','mk')",
        (uuid.uuid4().hex, asset, maker_asset, kind, side, price, status, t),
    )


def _prints(db, tokens):
    rows = db.execute(
        TableRead.TOKEN_PRINTS_CTE
        + "SELECT TOKEN_ID, MATCH_TIME, PRICE, TRADE_SIZE, SIDE FROM prints "
        "ORDER BY TOKEN_ID, MATCH_TIME",
        (list(tokens), list(tokens)),
    ).fetchall()
    return [(r["TOKEN_ID"], int(r["PRICE"]), r["SIDE"]) for r in rows]


def test_a_tape_print_prints_yes_and_no_summing_to_one_dollar(db):
    _trade(
        db,
        asset="y",
        maker_asset="n",
        kind="NORMAL",
        side="BUY",
        price=300_000,
        status="MIRRORED",
    )
    got = _prints(db, ["y", "n"])
    assert got == [("n", 700_000, "SELL"), ("y", 300_000, "BUY")]
    assert sum(p for _, p, _ in got) == MICRO


def test_a_tape_row_written_before_the_no_token_prints_once(db):
    _trade(
        db,
        asset="y",
        maker_asset="y",
        kind="NORMAL",
        side="SELL",
        price=250_000,
        status="MIRRORED",
    )
    assert _prints(db, ["y", "n"]) == [("y", 250_000, "SELL")]


def test_agent_fills_never_print(db):
    for kind, status in (
        ("NORMAL", "CONFIRMED"),
        ("MINT", "PENDING"),
        ("MERGE", "matched"),
        ("MINT", "FAILED"),
    ):
        _trade(
            db,
            asset="y",
            maker_asset="n",
            kind=kind,
            side="BUY",
            price=300_000,
            status=status,
        )
    assert _prints(db, ["y", "n"]) == []


def test_both_token_columns_are_indexed(db):
    """Without these the tape seq-scans the whole table on every chart load,
    measured at 132 ms over 458k rows to return 21 points."""
    defs = {
        r["INDEXNAME"]: r["INDEXDEF"]
        for r in db.execute(
            "SELECT indexname AS INDEXNAME, indexdef AS INDEXDEF FROM pg_indexes "
            "WHERE tablename='trades'"
        ).fetchall()
    }
    assert "idx_trades_asset_id" in defs
    assert "idx_trades_maker_asset_id" not in defs
    assert "MIRRORED" in defs["idx_trades_mirrored_maker_asset_id"]
