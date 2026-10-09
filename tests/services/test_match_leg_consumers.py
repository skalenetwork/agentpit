"""The read paths that still told a MINT's story with the wrong token or the
wrong price. The money was already right — this is the narration of it."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from agentpit.db.table_read import TableRead
from tests.db_helpers import fresh_test_conn


@pytest.fixture()
def db() -> Any:
    conn = fresh_test_conn()
    yield conn
    conn.close()


# ----- the activity feed ------------------------------------------------------

from agentpit.datastructures.condition_id import ConditionId  # noqa: E402
from agentpit.datastructures.create_market_request import (  # noqa: E402
    CreateMarketRequest,
)
from agentpit.datastructures.market_state import MarketState  # noqa: E402
from agentpit.db.table_write import TableWrite  # noqa: E402


def _market(db, seed="act"):
    return TableWrite.create_market(
        db,
        CreateMarketRequest(
            question=f"{seed}?",
            description="d",
            erc1155_tokens=[(f"{seed}-y", "Yes"), (f"{seed}-n", "No")],
            slug=seed,
            condition_id=ConditionId("0x" + seed.encode().hex().ljust(64, "0")[:64]),
            state=MarketState.ACTIVE,
        ),
        is_polygon_market=False,
    )


def _activity_rows(db, api_key, eth_address):
    """The trade half of list_activity, exercised through the same code."""
    from agentpit.services.account_service import AccountService
    return AccountService._trade_activity(db, api_key, eth_address)


def test_a_mint_makers_activity_names_the_token_it_received(db):
    m = _market(db, "act")
    db.execute(
        "INSERT INTO trades (TRADE_ID, MARKET, ASSET_ID, MAKER_ASSET_ID, "
        "MATCH_KIND, SIDE, PRICE, TRADE_SIZE, STATUS, MATCH_TIME, "
        "TAKER_API_KEY, MAKER_API_KEY) "
        "VALUES (%s,%s,'act-y','act-n','MINT','BUY',300000,100,'matched',10,"
        "'tk','mk')",
        (uuid.uuid4().hex, m.condition_id.value),
    )
    acts = _activity_rows(db, "mk", "0xmaker")
    assert len(acts) == 1
    a = acts[0]
    assert a.asset == "act-n", "the maker received the OTHER outcome"
    assert a.side == "BUY", "both sides of a mint ACQUIRE"
    assert a.price == 0.3, "the stored price IS the maker's"
    assert a.outcome == "No"
    assert a.usdcSize == pytest.approx(0.3 * a.size)


def test_the_mint_taker_sees_the_complement_of_the_stored_price(db):
    m = _market(db, "actt")
    db.execute(
        "INSERT INTO trades (TRADE_ID, MARKET, ASSET_ID, MAKER_ASSET_ID, "
        "MATCH_KIND, SIDE, PRICE, TRADE_SIZE, STATUS, MATCH_TIME, "
        "TAKER_API_KEY, MAKER_API_KEY) "
        "VALUES (%s,%s,'actt-y','actt-n','MINT','BUY',300000,100,'matched',10,"
        "'tk','mk')",
        (uuid.uuid4().hex, m.condition_id.value),
    )
    a = _activity_rows(db, "tk", "0xtaker")[0]
    assert a.asset == "actt-y"
    assert a.side == "BUY"
    assert a.price == 0.7


def test_a_self_matched_normal_row_shows_both_sides(db):
    """One account on both legs is the dominant shape on production."""
    m = _market(db, "acts")
    db.execute(
        "INSERT INTO trades (TRADE_ID, MARKET, ASSET_ID, MAKER_ASSET_ID, "
        "MATCH_KIND, SIDE, PRICE, TRADE_SIZE, STATUS, MATCH_TIME, "
        "TAKER_API_KEY, MAKER_API_KEY) "
        "VALUES (%s,%s,'acts-y','acts-y','NORMAL','BUY',250000,100,'matched',"
        "10,'same','same')",
        (uuid.uuid4().hex, m.condition_id.value),
    )
    acts = _activity_rows(db, "same", "0xsame")
    assert sorted(a.side for a in acts) == ["BUY", "SELL"]
    assert {a.price for a in acts} == {0.25}


# ----- /data/trades -----------------------------------------------------------


def _mint_row(db, condition_id):
    db.execute(
        "INSERT INTO trades (TRADE_ID, TAKER_ORDER_ID, MAKER_ORDERS, MARKET, "
        "ASSET_ID, MAKER_ASSET_ID, MATCH_KIND, SIDE, PRICE, TRADE_SIZE, "
        "STATUS, MATCH_TIME, BUCKET_INDEX, FEE_RATE_BPS, TAKER_API_KEY, "
        "MAKER_API_KEY) VALUES (%s,'o','[]',%s,'dt-y','dt-n','MINT','BUY',"
        "300000,100,'matched',10,0,0,'tk','mk')",
        (uuid.uuid4().hex, condition_id),
    )


def test_filtering_by_your_own_token_finds_your_maker_fill(db):
    m = _market(db, "dt")
    _mint_row(db, m.condition_id.value)
    rows = TableRead.list_trades_for_api_key(db, "mk", asset_id="dt-n")
    assert len(rows) == 1, "the maker's own token used to match nothing"


def test_the_taker_filter_is_unaffected(db):
    m = _market(db, "dt")
    _mint_row(db, m.condition_id.value)
    assert len(TableRead.list_trades_for_api_key(db, "tk", asset_id="dt-y")) == 1
    assert TableRead.list_trades_for_api_key(db, "tk", asset_id="dt-n") == []


def test_the_maker_perspective_reports_its_own_token_at_its_own_price(db):
    from agentpit.services.trade_service import TradeService
    m = _market(db, "dt")
    _mint_row(db, m.condition_id.value)
    rows = TableRead.list_trades_for_api_key(db, "mk")
    wire = TradeService._to_wire(db, rows[0], api_key="mk", user_id="1",
                                 eth_address="0xmaker")
    assert wire.asset_id == "dt-n"
    assert wire.price == "0.3", "PRICE is already the maker's — do not flip it"
    assert wire.side == "BUY"
    assert wire.outcome == "No"
    assert wire.trader_side == "MAKER"


def test_the_taker_perspective_flips_the_price_and_keeps_its_own_outcome(db):
    from agentpit.services.trade_service import TradeService
    m = _market(db, "dt")
    _mint_row(db, m.condition_id.value)
    rows = TableRead.list_trades_for_api_key(db, "tk")
    wire = TradeService._to_wire(db, rows[0], api_key="tk", user_id="2",
                                 eth_address="0xtaker")
    assert wire.asset_id == "dt-y"
    assert wire.price == "0.7"
    assert wire.outcome == "Yes", "used to report the maker's outcome label"
    assert wire.trader_side == "TAKER"


def _self_mint_row(db, condition_id):
    db.execute(
        "INSERT INTO trades (TRADE_ID, TAKER_ORDER_ID, MAKER_ORDERS, MARKET, "
        "ASSET_ID, MAKER_ASSET_ID, MATCH_KIND, SIDE, PRICE, TRADE_SIZE, "
        "STATUS, MATCH_TIME, BUCKET_INDEX, FEE_RATE_BPS, TAKER_API_KEY, "
        "MAKER_API_KEY) VALUES (%s,'o','[]',%s,'dtm-y','dtm-n','MINT','BUY',"
        "300000,100,'matched',10,0,0,'same','same')",
        (uuid.uuid4().hex, condition_id),
    )


def test_a_self_matched_mint_filtered_by_the_makers_token_returns_the_makers_leg(db):
    """Self-matching is the dominant production shape (373k of 458k rows):
    one api_key is BOTH taker and maker on the row, so `trader_side` is
    always TAKER (TAKER_API_KEY == api_key) and, without prefer_token, the
    taker leg would win even when the caller explicitly filtered by the
    maker's own token — asset_id would silently disagree with the filter
    that found the row."""
    from agentpit.services.trade_service import TradeService
    m = _market(db, "dtm")
    _self_mint_row(db, m.condition_id.value)
    rows = TableRead.list_trades_for_api_key(db, "same", asset_id="dtm-n")
    assert len(rows) == 1
    wire = TradeService._to_wire(
        db, rows[0], api_key="same", user_id="1", eth_address="0xsame",
        prefer_token="dtm-n",
    )
    assert wire.asset_id == "dtm-n"
    assert wire.price == "0.3", "the maker's own price, not the taker's flipped one"


def _normal_row(db, condition_id):
    db.execute(
        "INSERT INTO trades (TRADE_ID, TAKER_ORDER_ID, MAKER_ORDERS, MARKET, "
        "ASSET_ID, MATCH_KIND, SIDE, PRICE, TRADE_SIZE, STATUS, MATCH_TIME, "
        "BUCKET_INDEX, FEE_RATE_BPS, TAKER_API_KEY, MAKER_API_KEY) "
        "VALUES (%s,'o','[]',%s,'dtn-y','NORMAL','BUY',250000,100,'matched',"
        "10,0,0,'tk','mk')",
        (uuid.uuid4().hex, condition_id),
    )


def test_a_normal_row_keeps_the_makers_flipped_side_and_unflipped_price(db):
    """tests/onchain/test_data_trades.py drives this same NORMAL path end to
    end over real HTTP orders, but only asserts trader_side/owner — never the
    maker's flipped side, its unflipped price, or its outcome — and the
    prescribed suite run (`--ignore=tests/onchain`) skips that file entirely.
    This is the NORMAL-row regression check for _to_wire in the runnable
    suite, covering both perspectives on one row."""
    from agentpit.services.trade_service import TradeService
    m = _market(db, "dtn")
    _normal_row(db, m.condition_id.value)
    rows = TableRead.list_trades_for_api_key(db, "mk")
    maker = TradeService._to_wire(db, rows[0], api_key="mk", user_id="1",
                                   eth_address="0xmaker")
    assert maker.asset_id == "dtn-y", "same token as the taker's"
    assert maker.side == "SELL", "opposite of the row's BUY"
    assert maker.price == "0.25", "PRICE is unflipped on a NORMAL row"
    assert maker.outcome == "Yes"

    taker = TradeService._to_wire(db, rows[0], api_key="tk", user_id="2",
                                   eth_address="0xtaker")
    assert taker.asset_id == "dtn-y"
    assert taker.side == "BUY"
    assert taker.price == "0.25"
    assert taker.outcome == "Yes"
