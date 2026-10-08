from decimal import Decimal

import pytest

from agentpit.config import Settings
from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.market_state import MarketState
from agentpit.datastructures.place_order_request import PlaceOrderRequest
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import BusinessRuleError
from agentpit.services.order_service import OrderService
from tests.db_helpers import fresh_test_db

YES = "88" + "0" * 20
NO = "88" + "0" * 19 + "1"
COND = "0x" + "ab" * 32


class _ReachedChain(Exception):
    """Raised by the fake chain: every guard before the balance check passed."""


class _Chain:
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
    svc = OrderService(db, _Chain(), Settings(**settings))  # type: ignore[arg-type]
    return svc, db, user


def _req(side="BUY", price="0.5", size="1", order_type="GTC"):
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
    with pytest.raises(_ReachedChain):
        svc.place_order(bot, _req(size="1"))


def _rest_live_orders(db, api_key, n):
    with db.write() as conn:
        for i in range(n):
            conn.execute(
                "INSERT INTO orders (ORDER_ID, TOKEN_ID, SIDE, PRICE, STATUS, REMAINING_AMOUNT, EXPIRATION, CREATED_AT, API_KEY, ORDER_TYPE) "
                "VALUES (%s, %s, 'BUY', 100000, 'live', 1000000, 0, 0, %s, 'GTC')",
                (f"0xlive{i}", YES, api_key),
            )


def test_live_order_cap_refuses_a_resting_order():
    svc, db, user = _setup(AGENTPIT_MAX_LIVE_ORDERS_PER_ACCOUNT=2)
    _rest_live_orders(db, user.api_key, 2)
    with pytest.raises(BusinessRuleError, match="too many open orders"):
        svc.place_order(user, _req(size="10"))


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
    with pytest.raises(_ReachedChain):
        svc.place_order(bot, _req(size="10"))
