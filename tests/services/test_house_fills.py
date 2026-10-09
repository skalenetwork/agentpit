import threading
import time
from decimal import Decimal
from types import SimpleNamespace
from typing import Literal

import pytest

from agentpit.config import Settings
from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.market import Market
from agentpit.datastructures.market_state import MarketState
from agentpit.datastructures.place_order_request import PlaceOrderRequest
from agentpit.datastructures.user import User
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import (
    AdminGasPausedError,
    GasBudgetExceededError,
    OrderNotFilledError,
)
from agentpit.liquidity import feed
from agentpit.liquidity.replica import BookReplica, Used
from agentpit.onchain.deployment import Deployment
from agentpit.services.order_service import OrderService
from tests.db_helpers import fresh_test_db
from tests.fake_skaled import FakeFn, FakeSkaled, make_sender

_ADDR = "0x00000000000000000000000000000000000000a1"


def _service(db: DbSession, **settings) -> OrderService:
    sender, _, _ = make_sender(FakeSkaled())
    deployment = Deployment(
        chain_id=1,
        rpc_url="",
        admin=_ADDR,
        usd=_ADDR,
        faucet=_ADDR,
        ctf=_ADDR,
        proxy_factory=_ADDR,
        safe_factory=_ADDR,
        exchange=_ADDR,
        signup_grant_raw=0,
    )
    onchain = SimpleNamespace(
        _client=SimpleNamespace(admin_sender=sender, web3=None, deployment=deployment),
        _contracts=SimpleNamespace(
            exchange=SimpleNamespace(
                functions=SimpleNamespace(matchOrders=lambda *_: FakeFn())
            )
        ),
        usd_balance=lambda _address: 10**15,
        ctf_balance=lambda _address, _token: 10**15,
        check_sponsored=lambda: None,
    )
    return OrderService(db, onchain, Settings(**settings))  # type: ignore[arg-type]


def _world(db: DbSession) -> tuple[Market, User]:
    with db.write() as conn:
        market = TableWrite.create_market(
            conn,
            CreateMarketRequest(
                question="Will it rain?",
                description="d",
                erc1155_tokens=[("101", "Yes"), ("102", "No")],
                slug="rain",
                condition_id=ConditionId("0x" + "ab" * 32),
                state=MarketState.ACTIVE,
            ),
            is_polygon_market=False,
        )
        agent = TableRead.get_user_by_api_key(
            conn, TableWrite.create_user(conn, None, None)[2]
        )
    assert agent is not None
    return market, agent


def _place(
    svc: OrderService,
    agent: User,
    token: str,
    side: Literal["BUY", "SELL"],
    price: str,
    size: str,
    kind: Literal["GTC", "FOK", "FAK", "GTD"] = "FAK",
):
    return svc.place_order(
        agent,
        PlaceOrderRequest(
            token_id=token,
            side=side,
            price=Decimal(price),
            size=Decimal(size),
            order_type=kind,
        ),
    )


def _row(db: DbSession, order_id: str) -> tuple[str, int]:
    with db.read() as conn:
        r = conn.execute(
            "SELECT STATUS, REMAINING_AMOUNT FROM orders WHERE ORDER_ID = %s",
            (order_id,),
        ).fetchone()
    return r["STATUS"], r["REMAINING_AMOUNT"]


def _ask(book: BookReplica, price: str, size: str) -> None:
    book.apply_price_change_entry(
        {"asset_id": book.asset_id, "side": "SELL", "price": price, "size": size}
    )


def test_a_fill_trades_with_the_house_at_polymarkets_level(house_book):
    db = fresh_test_db()
    _, agent = _world(db)
    book = house_book("101", bids=(("0.38", "50"),), asks=(("0.6", "20"),))
    house = feed.HOUSE.user
    svc = _service(db)

    bought = _place(svc, agent, "101", "BUY", "0.65", "10")
    sold = _place(svc, agent, "102", "SELL", "0.38", "5")

    assert (
        bought.success,
        bought.status,
        bought.makingAmount,
        bought.takingAmount,
    ) == (True, "matched", "6", "10")
    assert (sold.success, sold.makingAmount, sold.takingAmount) == (True, "5", "2")
    with db.read() as conn:
        trades = conn.execute(
            "SELECT TAKER_ORDER_ID, MATCH_KIND, PRICE, ASSET_ID, MAKER_ASSET_ID, MAKER_API_KEY, TRADE_SIZE, STATUS "
            "FROM trades ORDER BY MATCH_KIND"
        ).fetchall()
        houses = conn.execute(
            "SELECT TOKEN_ID, SIDE, STATUS, REMAINING_AMOUNT FROM orders WHERE API_KEY = %s ORDER BY TOKEN_ID",
            (house.api_key,),
        ).fetchall()
    assert [tuple(t.values()) for t in trades] == [
        (
            bought.orderID,
            "MINT",
            400_000,
            "101",
            "102",
            house.api_key,
            10_000_000,
            "CONFIRMED",
        ),
        (
            sold.orderID,
            "NORMAL",
            400_000,
            "102",
            "102",
            house.api_key,
            5_000_000,
            "CONFIRMED",
        ),
    ]
    assert [tuple(h.values()) for h in houses] == [
        ("102", "BUY", "matched", 0),
        ("102", "BUY", "matched", 0),
    ]
    assert book.used == {(agent.api_key, True, 600_000): Used(20_000_000, 15_000_000)}


def test_a_killed_order_writes_nothing_and_uses_nothing_up(house_book):
    db = fresh_test_db()
    _, agent = _world(db)
    book = house_book("101", asks=(("0.6", "5"),))
    svc = _service(db)

    with pytest.raises(OrderNotFilledError):
        _place(svc, agent, "101", "BUY", "0.6", "10", kind="FOK")

    with db.read() as conn:
        counts = conn.execute(
            "SELECT (SELECT COUNT(*) FROM orders) o, (SELECT COUNT(*) FROM trades) t"
        ).fetchone()
    assert (counts["o"], counts["t"], book.used) == (0, 0, {})


def test_the_sweeper_fills_crossed_orders_oldest_first_within_the_agents_use_up(
    house_book,
):
    db = fresh_test_db()
    _, agent = _world(db)
    book = house_book("101", asks=(("0.6", "5"),))
    svc = _service(db)
    old = _place(svc, agent, "101", "BUY", "0.55", "4", kind="GTC")
    new = _place(svc, agent, "101", "BUY", "0.55", "4", kind="GTC")
    low = _place(svc, agent, "101", "BUY", "0.5", "4", kind="GTC")
    with db.write() as conn:
        conn.execute(
            "UPDATE orders SET CREATED_AT = CREATED_AT - 10 WHERE ORDER_ID = %s",
            (old.orderID,),
        )

    svc.sweep()
    assert [_row(db, o.orderID) for o in (old, new, low)] == [("live", 4_000_000)] * 3

    _ask(book, "0.55", "5")
    svc.sweep()
    svc.sweep()

    assert [_row(db, o.orderID) for o in (old, new, low)] == [
        ("matched", 0),
        ("live", 3_000_000),
        ("live", 4_000_000),
    ]
    with db.read() as conn:
        statuses = [
            r["STATUS"] for r in conn.execute("SELECT STATUS FROM trades").fetchall()
        ]
    assert statuses == ["PENDING", "PENDING"]


def test_a_cancel_racing_the_sweeper_cancels_exactly_the_remainder(
    house_book, monkeypatch
):
    db = fresh_test_db()
    _, agent = _world(db)
    book = house_book("101", asks=(("0.6", "4"),))
    svc = _service(db)
    order = _place(svc, agent, "101", "BUY", "0.55", "10", kind="GTC")
    _ask(book, "0.55", "4")
    take = svc._take
    racers: list[threading.Thread] = []
    results = []

    def take_then_race(*args):
        match = take(*args)
        racers.append(
            threading.Thread(
                target=lambda: results.append(svc.cancel_orders(agent, [order.orderID]))
            )
        )
        racers[0].start()
        time.sleep(0.3)
        return match

    monkeypatch.setattr(svc, "_take", take_then_race)
    svc.sweep()
    racers[0].join()

    assert [r.canceled for r in results] == [[order.orderID]]
    assert _row(db, order.orderID) == ("cancelled", 6_000_000)


def test_a_market_that_left_active_never_fills(house_book):
    db = fresh_test_db()
    market, agent = _world(db)
    book = house_book("101", asks=(("0.6", "4"),))
    svc = _service(db)
    order = _place(svc, agent, "101", "BUY", "0.55", "4", kind="GTC")
    with db.write() as conn:
        TableWrite.set_market_state(
            conn, market.market_id, MarketState.ACTIVE, MarketState.CLOSED, None
        )
    _ask(book, "0.55", "4")

    svc.sweep()

    assert (_row(db, order.orderID), book.used) == (("live", 4_000_000), {})


def _paused() -> None:
    raise AdminGasPausedError()


def _counts(db: DbSession) -> tuple[int, int]:
    with db.read() as conn:
        row = conn.execute(
            "SELECT (SELECT COUNT(*) FROM orders) o, (SELECT COUNT(*) FROM trades) t"
        ).fetchone()
    return row["o"], row["t"]


def _gas_today(db: DbSession, agent: User) -> int:
    with db.read() as conn:
        return TableRead.sponsored_gas_used(conn, agent.api_key, int(time.time()) // 86_400)


def _spend_today(db: DbSession, agent: User, gas: int) -> None:
    with db.write() as conn:
        TableWrite.add_sponsored_gas(conn, agent.api_key, int(time.time()) // 86_400, gas)


def test_a_paused_breaker_makes_the_sweeper_skip_and_resting_orders_rest(house_book):
    db = fresh_test_db()
    _, agent = _world(db)
    book = house_book("101", asks=(("0.6", "5"),))
    svc = _service(db)
    order = _place(svc, agent, "101", "BUY", "0.55", "4", kind="GTC")
    _ask(book, "0.55", "5")
    svc._onchain.check_sponsored = _paused

    svc.sweep()

    assert (_row(db, order.orderID), _counts(db), book.used) == (
        ("live", 4_000_000),
        (1, 0),
        {},
    )


def test_every_fill_charges_the_agents_day_a_flat_250k_gas(house_book):
    db = fresh_test_db()
    _, agent = _world(db)
    book = house_book("101", asks=(("0.6", "5"),))
    svc = _service(db)
    _place(svc, agent, "101", "BUY", "0.6", "2")
    _place(svc, agent, "101", "BUY", "0.55", "2", kind="GTC")
    _ask(book, "0.55", "5")

    svc.sweep()

    assert _gas_today(db, agent) == 2 * 250_000


def test_a_placement_fill_over_the_daily_budget_rolls_the_whole_placement_back(house_book):
    """The pre-check passed (a race with another placement), so the fill's own
    reservation refuses it: no order row, no trade, nothing used up."""
    db = fresh_test_db()
    _, agent = _world(db)
    book = house_book("101", asks=(("0.6", "20"),))
    svc = _service(db, AGENTPIT_DAILY_SPONSORED_GAS_PER_ACCOUNT=1_000_000)
    svc._check_gas_budget = lambda _user: None
    _spend_today(db, agent, 1_000_000)

    with pytest.raises(GasBudgetExceededError) as info:
        _place(svc, agent, "101", "BUY", "0.6", "10")

    assert 0 < info.value.retry_after <= 86_400
    assert (_counts(db), book.used, _gas_today(db, agent)) == ((0, 0), {}, 1_000_000)


def test_a_sweeper_fill_over_the_daily_budget_is_skipped_and_the_order_rests(house_book):
    db = fresh_test_db()
    _, agent = _world(db)
    book = house_book("101", asks=(("0.6", "5"),))
    svc = _service(db, AGENTPIT_DAILY_SPONSORED_GAS_PER_ACCOUNT=1_000_000)
    order = _place(svc, agent, "101", "BUY", "0.55", "4", kind="GTC")
    _spend_today(db, agent, 1_000_000)
    _ask(book, "0.55", "5")

    svc.sweep()

    assert (_row(db, order.orderID), _counts(db), book.used) == (
        ("live", 4_000_000),
        (1, 0),
        {},
    )
