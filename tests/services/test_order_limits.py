import json
import time
from decimal import Decimal
from types import SimpleNamespace
from typing import Literal
from unittest.mock import patch

import pytest

from agentpit.config import Settings
from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.market_state import MarketState
from agentpit.datastructures.place_order_request import PlaceOrderRequest
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import AdminGasPausedError, BusinessRuleError, GasBudgetExceededError
from agentpit.onchain.order_signer import OrderData
from agentpit.services.order_service import OrderService
from tests.db_helpers import fresh_test_db
from tests.fake_skaled import FakeFn, FakeSkaled, make_sender

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
                # CLOSED first: a RESOLVED request must carry its outcome, which
                # the UPDATE below supplies.
                slug=slug, condition_id=ConditionId("0x" + "cd" * 32), state=MarketState.CLOSED,
            ),
            is_polygon_market=False,
        )
        conn.execute(
            "UPDATE markets SET MARKET_STATE = %s, RESOLVED_OUTCOME = %s WHERE SLUG = %s",
            (state.value, 0 if state is MarketState.RESOLVED else None, slug),
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
        assert TableRead.count_live_orders(conn, user.api_key) == 2
    with pytest.raises(BusinessRuleError, match="2 are live"):
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


def _booked_rows(db, api_key) -> int:
    with db.read() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM sponsored_gas WHERE API_KEY = %s", (api_key,)).fetchone()["n"]


def test_recording_zero_gas_writes_no_row():
    svc, db, user = _setup()
    svc._record_sponsored_gas(user, 0)
    svc._record_sponsored_gas(user, -5)
    assert _booked_rows(db, user.api_key) == 0
    svc._record_sponsored_gas(user, 70_000)         # the guard is not just a no-op
    assert _booked_rows(db, user.api_key) == 1


def _taker_and_matches(n_groups):
    """A taker order and one match per kind (NORMAL, then MINT), so each match
    is its own `matchOrders` group."""
    zero = "0x" + "00" * 20
    taker = OrderData(
        salt=1, maker=zero, signer=zero, taker=zero, tokenId=int(YES), makerAmount=5_000_000,
        takerAmount=10_000_000, expiration=0, nonce=0, feeRateBps=0, side=0, signatureType=0,
    )
    maker_json = json.dumps({
        "salt": 2, "maker": zero, "signer": zero, "taker": zero, "tokenId": int(YES),
        "makerAmount": 10_000_000, "takerAmount": 5_000_000, "expiration": 0, "nonce": 0,
        "feeRateBps": 0, "side": 1, "signatureType": 0, "signature": "0x00",
    })
    matches = [
        {"match_kind": kind, "trade_size": 10_000_000, "maker_row": {"ORDER_JSON": maker_json}}
        for kind in ("NORMAL", "MINT")[:n_groups]
    ]
    return taker, matches


def _settle(receipts):
    """Run `_settle_on_chain` against fake admin sends that return `receipts`
    in order, one per match-kind group. Returns (tx hashes, gas_used)."""
    taker, matches = _taker_and_matches(len(receipts))
    exchange = SimpleNamespace(functions=SimpleNamespace(matchOrders=lambda *args: ("matchOrders", args)))
    onchain = SimpleNamespace(_client=object(), _contracts=SimpleNamespace(exchange=exchange))
    svc = OrderService(None, onchain, Settings())  # type: ignore[arg-type]
    sent = iter(receipts)
    gas_used: list[int] = []
    with patch("agentpit.services.order_service.send_admin_tx", lambda *_a, **_k: next(sent)):
        hashes = svc._settle_on_chain(taker, b"\x00", matches, gas_used)
    return hashes, gas_used


def test_receipt_without_gas_used_counts_zero():
    hashes, gas_used = _settle([{"transactionHash": b"\x01", "status": 1}])
    assert hashes == [b"\x01"]
    assert gas_used == [0]


def test_reverted_receipt_still_counts_its_gas():
    hashes, gas_used = _settle([{"transactionHash": b"\x02", "status": 0, "gasUsed": 123_456}])
    assert hashes == [b"\x02"]
    assert gas_used == [123_456]


def test_each_group_is_counted_even_when_one_receipt_lacks_gas():
    _hashes, gas_used = _settle([
        {"transactionHash": b"\x03", "status": 1, "gasUsed": 80_000},
        {"transactionHash": b"\x04", "status": 1},
    ])
    assert gas_used == [80_000, 0]


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


def test_an_admitted_placement_settles_even_when_the_breaker_trips_mid_way():
    # `place_order` checks the breaker once, before any order row exists; after
    # that the placement is admitted as a whole. A real sender over a fake node,
    # funded to pass that check but not to pay for two groups: the first group's
    # receipt takes the balance below the stop level, and the second group (and
    # a concurrent debit could do the same before the first) must still go out,
    # or the DB would call a group FAILED that is already settled on chain.
    chain = FakeSkaled()
    sender, account, _clock = make_sender(chain, stop_gas=100)
    price = chain.fee[0]
    chain.balances[account.address] = (100 + 60_000) * price - 1   # 1 wei short once one group is paid
    sender.refresh_gas_balance()
    sender.check_sponsored()                                           # admitted

    taker, matches = _taker_and_matches(2)
    exchange = SimpleNamespace(functions=SimpleNamespace(matchOrders=lambda *_a: FakeFn()))
    onchain = SimpleNamespace(
        _client=SimpleNamespace(admin_sender=sender), _contracts=SimpleNamespace(exchange=exchange)
    )
    svc = OrderService(None, onchain, Settings())  # type: ignore[arg-type]
    gas_used: list[int] = []
    hashes = svc._settle_on_chain(taker, b"\x00", matches, gas_used)

    assert len(hashes) == 2 and len(chain.accepted) == 2
    assert sender.gas_state() == "paused"      # the breaker really did trip in between
