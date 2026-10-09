import time
from decimal import Decimal
from typing import Literal

import pytest

from agentpit.config import Settings
from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.market_state import MarketState
from agentpit.datastructures.place_order_request import PlaceOrderRequest
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import AdminGasPausedError, BusinessRuleError, GasBudgetExceededError
from agentpit.services.order_service import OrderService
from tests.db_helpers import fresh_test_db

YES = "88" + "0" * 20
NO = "88" + "0" * 19 + "1"
COND = "0x" + "ab" * 32


class _ReachedChain(Exception):
    """Raised by the fake chain: every guard before the balance check passed."""


class _Chain:
    def check_sponsored(self):
        """The breaker is closed, so `_ReachedChain` still means the balance
        check was reached, not that the breaker guard ran."""

    def __getattr__(self, name):
        raise _ReachedChain(name)


def _setup(**settings):
    db = fresh_test_db()
    with db.write() as conn:
        TableWrite.create_market(
            conn,
            CreateMarketRequest(
                question="Guards?", description="d", erc1155_tokens=[(YES, "Yes"), (NO, "No")],
                slug="guards", condition_id=ConditionId(COND), state=MarketState.ACTIVE,
            ),
            is_polygon_market=False,
        )
        user_id, _acct, api_key = TableWrite.create_user(conn, email="limits@example.com", password_hash=None, handle=None)
        user = TableRead.get_user_by_userid(conn, user_id)
        assert user is not None
    svc = OrderService(db, _Chain(), Settings(**settings))  # type: ignore[arg-type]
    return svc, db, user


def _req(
    side: Literal["BUY", "SELL"] = "BUY",
    price="0.5",
    size="1",
    order_type: Literal["GTC", "FOK", "FAK", "GTD"] = "GTC",
):
    return PlaceOrderRequest(token_id=YES, side=side, price=Decimal(price), size=Decimal(size), order_type=order_type)


def test_below_one_dollar_is_rejected_before_the_chain():
    svc, _db, user = _setup(AGENTPIT_MIN_ORDER_NOTIONAL_MICRO=1_000_000)
    with pytest.raises(BusinessRuleError, match="too small"):
        svc.place_order(user, _req(price="0.5", size="1.999998"))   # $0.999999


def test_sell_notional_counts_the_collateral_leg():
    svc, _db, user = _setup(AGENTPIT_MIN_ORDER_NOTIONAL_MICRO=1_000_000)
    with pytest.raises(BusinessRuleError, match="too small"):
        svc.place_order(user, _req(side="SELL", price="0.5", size="1"))


def test_exactly_one_dollar_is_accepted():
    svc, _db, user = _setup(AGENTPIT_MIN_ORDER_NOTIONAL_MICRO=1_000_000)
    with pytest.raises(_ReachedChain):
        svc.place_order(user, _req(price="0.1", size="10"))


def test_the_house_is_exempt_from_the_minimum():
    svc, db, user = _setup(AGENTPIT_MIN_ORDER_NOTIONAL_MICRO=1_000_000)
    with db.write() as conn:
        TableWrite.mark_user_as_bot(conn, user.api_key)
        bot = TableRead.get_user_by_userid(conn, user.user_id)
        assert bot is not None
    with pytest.raises(_ReachedChain):
        svc.place_order(bot, _req(size="1"))


def _rest_live_orders(db, api_key, n, token=YES):
    with db.write() as conn:
        for i in range(n):
            conn.execute(
                "INSERT INTO orders (ORDER_ID, TOKEN_ID, SIDE, PRICE, STATUS, REMAINING_AMOUNT, EXPIRATION, CREATED_AT, API_KEY, ORDER_TYPE) "
                "VALUES (%s, %s, 'BUY', 100000, 'live', 1000000, 0, 0, %s, 'GTC')",
                (f"0xlive{token[:2]}{token[-1]}{i}", token, api_key),
            )


def _add_market(db, state, yes, no, slug):
    with db.write() as conn:
        TableWrite.create_market(
            conn,
            CreateMarketRequest(
                question=slug, description="d", erc1155_tokens=[(yes, "Yes"), (no, "No")],
                # CLOSED first: a RESOLVED market must carry its payouts, which
                # the UPDATE below supplies.
                slug=slug, condition_id=ConditionId("0x" + "cd" * 32), state=MarketState.CLOSED,
            ),
            is_polygon_market=False,
        )
        conn.execute(
            "UPDATE markets SET MARKET_STATE = %s, PAYOUTS = %s WHERE SLUG = %s",
            (state.value, [1, 0] if state is MarketState.RESOLVED else None, slug),
        )


def test_live_order_cap_refuses_a_resting_order():
    svc, db, user = _setup(AGENTPIT_MAX_LIVE_ORDERS_PER_ACCOUNT=2)
    _rest_live_orders(db, user.api_key, 2)
    with pytest.raises(BusinessRuleError, match="too many open orders"):
        svc.place_order(user, _req(size="10"))


def test_live_order_cap_counts_either_outcome_of_an_active_market():
    svc, db, user = _setup(AGENTPIT_MAX_LIVE_ORDERS_PER_ACCOUNT=2)
    _rest_live_orders(db, user.api_key, 2, token=NO)
    with pytest.raises(BusinessRuleError, match="too many open orders"):
        svc.place_order(user, _req(size="10"))


@pytest.mark.parametrize("state", [MarketState.CLOSED, MarketState.RESOLVED, MarketState.CANCELLED])
def test_resting_orders_on_a_dead_market_do_not_use_up_the_cap(state):
    # Closing, resolving or cancelling a market leaves its resting orders behind,
    # and takers are refused there: they can neither fill nor be usefully
    # cancelled, so they must not hold the account's slots forever.
    dead_yes, dead_no = "77" + "0" * 20, "77" + "0" * 19 + "1"
    svc, db, user = _setup(AGENTPIT_MAX_LIVE_ORDERS_PER_ACCOUNT=2)
    _add_market(db, state, dead_yes, dead_no, "dead")
    _rest_live_orders(db, user.api_key, 2, token=dead_yes)
    _rest_live_orders(db, user.api_key, 2, token=dead_no)
    with pytest.raises(_ReachedChain):
        svc.place_order(user, _req(size="10"))


def test_dead_market_orders_are_not_counted_but_active_ones_still_are():
    dead_yes, dead_no = "77" + "0" * 20, "77" + "0" * 19 + "1"
    svc, db, user = _setup(AGENTPIT_MAX_LIVE_ORDERS_PER_ACCOUNT=2)
    _add_market(db, MarketState.CLOSED, dead_yes, dead_no, "dead")
    _rest_live_orders(db, user.api_key, 5, token=dead_yes)
    _rest_live_orders(db, user.api_key, 2, token=YES)
    with db.read() as conn:
        assert TableRead.count_live_orders(conn, user.api_key) == 7                # every live order
        assert TableRead.count_live_orders_on_active_markets(conn, user.api_key) == 2
    with pytest.raises(BusinessRuleError, match="2 are live"):
        svc.place_order(user, _req(size="10"))


def _spy_on_active_count(monkeypatch):
    """Count the calls to the (expensive) ACTIVE-market count."""
    calls = []
    real = TableRead.count_live_orders_on_active_markets

    def spy(conn, api_key):
        calls.append(api_key)
        return real(conn, api_key)

    monkeypatch.setattr(TableRead, "count_live_orders_on_active_markets", staticmethod(spy))
    return calls


def test_the_active_market_count_only_runs_once_the_plain_count_reaches_the_cap(monkeypatch):
    # Expanding ERC1155_TOKENS for every ACTIVE market cost 17-36 ms per call on
    # the dev DB (4,685 ACTIVE markets) against 0.04 ms for the plain count, and
    # it ran on every GTC/GTD placement. The plain count never undercounts, so
    # below the cap it already settles the question.
    calls = _spy_on_active_count(monkeypatch)
    svc, db, user = _setup(AGENTPIT_MAX_LIVE_ORDERS_PER_ACCOUNT=3)
    _rest_live_orders(db, user.api_key, 2)
    with pytest.raises(_ReachedChain):
        svc.place_order(user, _req(size="10"))
    assert calls == []


def test_the_active_market_count_decides_once_the_plain_count_reaches_the_cap(monkeypatch):
    calls = _spy_on_active_count(monkeypatch)
    dead_yes, dead_no = "77" + "0" * 20, "77" + "0" * 19 + "1"
    svc, db, user = _setup(AGENTPIT_MAX_LIVE_ORDERS_PER_ACCOUNT=3)
    _add_market(db, MarketState.CANCELLED, dead_yes, dead_no, "dead")
    _rest_live_orders(db, user.api_key, 3, token=dead_yes)               # plain count 3 = the cap
    with pytest.raises(_ReachedChain):                                   # ... but none of them can fill
        svc.place_order(user, _req(size="10"))
    assert calls == [user.api_key]
    _rest_live_orders(db, user.api_key, 3, token=YES)                    # now 3 on an ACTIVE market
    with pytest.raises(BusinessRuleError, match="3 are live"):
        svc.place_order(user, _req(size="10"))
    assert calls == [user.api_key, user.api_key]


def test_live_order_cap_lets_fak_and_fok_through():
    svc, db, user = _setup(AGENTPIT_MAX_LIVE_ORDERS_PER_ACCOUNT=2)
    _rest_live_orders(db, user.api_key, 2)
    for order_type in ("FAK", "FOK"):
        with pytest.raises(_ReachedChain):
            svc.place_order(user, _req(size="10", order_type=order_type))


def test_live_order_cap_exempts_the_house():
    svc, db, user = _setup(AGENTPIT_MAX_LIVE_ORDERS_PER_ACCOUNT=2)
    _rest_live_orders(db, user.api_key, 2)
    with db.write() as conn:
        TableWrite.mark_user_as_bot(conn, user.api_key)
        bot = TableRead.get_user_by_userid(conn, user.user_id)
        assert bot is not None
    with pytest.raises(_ReachedChain):
        svc.place_order(bot, _req(size="10"))


def _spend(db, api_key, gas):
    with db.write() as conn:
        TableWrite.add_sponsored_gas(conn, api_key, int(time.time()) // 86_400, gas)


def test_exhausted_budget_is_refused_with_retry_after():
    svc, db, user = _setup(AGENTPIT_DAILY_SPONSORED_GAS_PER_ACCOUNT=1_000)
    _spend(db, user.api_key, 1_000)
    with pytest.raises(GasBudgetExceededError) as info:
        svc.place_order(user, _req(size="10"))
    assert 0 < info.value.retry_after <= 86_400


def test_budget_not_yet_spent_lets_the_order_through():
    svc, db, user = _setup(AGENTPIT_DAILY_SPONSORED_GAS_PER_ACCOUNT=1_000)
    _spend(db, user.api_key, 999)
    with pytest.raises(_ReachedChain):
        svc.place_order(user, _req(size="10"))


def test_the_house_has_no_budget():
    svc, db, user = _setup(AGENTPIT_DAILY_SPONSORED_GAS_PER_ACCOUNT=1_000)
    _spend(db, user.api_key, 5_000)
    with db.write() as conn:
        TableWrite.mark_user_as_bot(conn, user.api_key)
        bot = TableRead.get_user_by_userid(conn, user.user_id)
        assert bot is not None
    with pytest.raises(_ReachedChain):
        svc.place_order(bot, _req(size="10"))


class _PausedChain:
    def check_sponsored(self):
        raise AdminGasPausedError()

    def __getattr__(self, name):
        raise _ReachedChain(name)


def test_paused_breaker_refuses_before_any_order_row():
    _svc, db, user = _setup()
    svc = OrderService(db, _PausedChain(), Settings())  # type: ignore[arg-type]
    with pytest.raises(AdminGasPausedError):
        svc.place_order(user, _req(size="10"))
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) AS N FROM orders").fetchone()["N"] == 0


def test_paused_breaker_stops_the_house_too():
    _svc, db, user = _setup()
    with db.write() as conn:
        TableWrite.mark_user_as_bot(conn, user.api_key)
        bot = TableRead.get_user_by_userid(conn, user.user_id)
        assert bot is not None
    svc = OrderService(db, _PausedChain(), Settings())  # type: ignore[arg-type]
    with pytest.raises(AdminGasPausedError):
        svc.place_order(bot, _req(size="10"))


def test_the_house_reserves_nothing():
    svc, db, user = _setup(AGENTPIT_DAILY_SPONSORED_GAS_PER_ACCOUNT=1_000)
    with db.write() as conn:
        TableWrite.mark_user_as_bot(conn, user.api_key)
        bot = TableRead.get_user_by_userid(conn, user.user_id)
        assert bot is not None
        svc._reserve_sponsored_gas(conn, bot)
    with db.read() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM sponsored_gas").fetchone()["n"] == 0
