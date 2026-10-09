import time
from decimal import Decimal
from types import SimpleNamespace

import pytest
from eth_account import Account

from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.market_state import MarketState
from agentpit.datastructures.place_order_request import PlaceOrderRequest
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import MarketStateError
from agentpit.services.market_service import transition
from agentpit.services.order_service import OrderService
from tests.db_helpers import fresh_test_db


def _tokens(name: str) -> tuple[str, str]:
    n = int.from_bytes(name.encode(), "big")
    return f"{n}1", f"{n}2"


def _market(db, name: str, state: MarketState = MarketState.ACTIVE) -> int:
    yes, no = _tokens(name)
    with db.write() as conn:
        return TableWrite.create_market(
            conn,
            CreateMarketRequest(
                question=f"{name}?",
                description="d",
                erc1155_tokens=[(yes, "Yes"), (no, "No")],
                condition_id=ConditionId("0x" + name.encode().hex().ljust(64, "0")),
                state=state,
                start_date=1,
            ),
            is_polygon_market=False,
        ).market_id


def _order(db, order_id: str, token: str, api_key: str) -> None:
    with db.write() as conn:
        conn.execute(
            "INSERT INTO orders (ORDER_ID, TOKEN_ID, SIDE, PRICE, STATUS, "
            "REMAINING_AMOUNT, EXPIRATION, CREATED_AT, API_KEY) "
            "VALUES (%s, %s, 'BUY', 500000, 'live', 1000000, 0, %s, %s)",
            (order_id, token, int(time.time()), api_key),
        )


def _statuses(db) -> dict[str, str]:
    with db.read() as conn:
        rows = conn.execute("SELECT ORDER_ID, STATUS FROM orders").fetchall()
    return {r["ORDER_ID"]: r["STATUS"] for r in rows}


def test_closing_cancels_every_live_order_on_both_tokens_and_no_other():
    db = fresh_test_db()
    closing, other = _market(db, "closing"), _market(db, "other")
    _order(db, "yes-user", _tokens("closing")[0], "user")
    _order(db, "no-house", _tokens("closing")[1], "house")
    _order(db, "elsewhere", _tokens("other")[0], "user")

    assert transition(db, closing, MarketState.ACTIVE, MarketState.CLOSED)

    assert _statuses(db) == {"yes-user": "cancelled", "no-house": "cancelled", "elsewhere": "live"}
    with db.read() as conn:
        assert TableRead.get_market_state(conn, closing) == MarketState.CLOSED
        assert TableRead.get_market_state(conn, other) == MarketState.ACTIVE


def test_a_lost_race_changes_nothing():
    db = fresh_test_db()
    market_id = _market(db, "raced", MarketState.CLOSED)
    _order(db, "resting", _tokens("raced")[0], "user")

    assert not transition(db, market_id, MarketState.ACTIVE, MarketState.CLOSED)
    assert _statuses(db) == {"resting": "live"}
    with db.read() as conn:
        assert TableRead.get_market_state(conn, market_id) == MarketState.CLOSED


def test_resolving_stores_the_split_and_its_time_and_revalues_holders(monkeypatch):
    db = fresh_test_db()
    market_id = _market(db, "split", MarketState.CLOSED)
    touched = []
    monkeypatch.setattr(
        "agentpit.services.market_service.touch_holders", lambda: touched.append(market_id)
    )

    assert transition(db, market_id, MarketState.CLOSED, MarketState.RESOLVED, (1, 1))

    with db.read() as conn:
        market = TableRead.read_market(conn, market_id)
    assert market is not None and market.resolved_at is not None
    assert (market.market_state, market.payouts, market.winner) == (MarketState.RESOLVED, (1, 1), None)
    assert touched == [market_id]


def test_reentering_active_clears_the_book_and_keeps_the_market_open():
    db = fresh_test_db()
    market_id = _market(db, "kickoff")
    _order(db, "pre-game", _tokens("kickoff")[1], "user")

    assert transition(db, market_id, MarketState.ACTIVE, MarketState.ACTIVE)

    assert _statuses(db) == {"pre-game": "cancelled"}
    with db.read() as conn:
        assert TableRead.get_market_state(conn, market_id) == MarketState.ACTIVE


def test_an_order_is_refused_when_its_market_closed_before_the_match_commit():
    db = fresh_test_db()
    market_id = _market(db, "late")
    transition(db, market_id, MarketState.ACTIVE, MarketState.CLOSED)
    account = Account.create()
    user = SimpleNamespace(api_key="k", eth_address=account.address, eth_key=account, is_bot=False)
    deployment = SimpleNamespace(chain_id=31337, exchange="0x" + "11" * 20)
    onchain = SimpleNamespace(
        _client=SimpleNamespace(deployment=deployment),
        usd_balance=lambda _address: 10**12,
        check_sponsored=lambda: None,
    )
    service = OrderService(db, onchain)  # type: ignore[arg-type]
    order = PlaceOrderRequest(
        token_id=_tokens("late")[0], side="BUY", price=Decimal("0.5"), size=Decimal("10")
    )

    with pytest.raises(MarketStateError, match="is closed"):
        service.place_order(user, order)  # type: ignore[arg-type]
    assert _statuses(db) == {}
