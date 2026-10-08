import json
import secrets
import time
from decimal import Decimal
from types import SimpleNamespace
from typing import Literal
from unittest.mock import patch

import pytest
from web3.exceptions import TimeExhausted

from agentpit.config import Settings
from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.market_state import MarketState
from agentpit.datastructures.place_order_request import PlaceOrderRequest
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import AdminGasPausedError, BusinessRuleError, GasBudgetExceededError
from agentpit.onchain.deployment import Deployment
from agentpit.onchain.order_signer import OrderData
from agentpit.onchain.tx_sender import TxDropped
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


def _booked_rows(db, api_key) -> int:
    with db.read() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM sponsored_gas WHERE API_KEY = %s", (api_key,)).fetchone()["n"]


def test_booking_nothing_writes_no_row():
    svc, db, user = _setup()
    svc._book_sponsored_gas(user, [], 0)
    svc._book_sponsored_gas(user, [(0, ["maker-key"])], 0)              # a receipt without gasUsed
    svc._book_sponsored_gas(user, [(70_000, ["maker-key"])], 70_000)    # the reservation was exact
    assert _booked_rows(db, user.api_key) == 0
    svc._book_sponsored_gas(user, [(70_000, ["maker-key"])], 0)         # the guard is not just a no-op
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
        {
            "match_kind": kind, "trade_size": 10_000_000,
            "maker_row": {"ORDER_JSON": maker_json, "API_KEY": f"maker-{kind.lower()}"},
        }
        for kind in ("NORMAL", "MINT")[:n_groups]
    ]
    return taker, matches


def _settle(receipts, gas_used: "list[tuple[int, list[str]]] | None" = None):
    """Run `_settle_on_chain` against fake admin sends that return `receipts`
    in order, one per match-kind group. Returns (tx hashes, gas_used), gas_used
    holding one (receipt gasUsed, [maker API keys]) per group. Pass `gas_used`
    to read it after a settlement that raises."""
    taker, matches = _taker_and_matches(len(receipts))
    exchange = SimpleNamespace(functions=SimpleNamespace(matchOrders=lambda *args: ("matchOrders", args)))
    onchain = SimpleNamespace(_client=object(), _contracts=SimpleNamespace(exchange=exchange))
    svc = OrderService(None, onchain, Settings())  # type: ignore[arg-type]
    sent = iter(receipts)
    gas_used = [] if gas_used is None else gas_used
    with patch("agentpit.services.order_service.send_admin_tx", lambda *_a, **_k: next(sent)):
        hashes = svc._settle_on_chain(taker, b"\x00", matches, gas_used)
    return hashes, gas_used


def test_receipt_without_gas_used_counts_zero():
    hashes, gas_used = _settle([{"transactionHash": b"\x01", "status": 1}])
    assert hashes == [b"\x01"]
    assert gas_used == [(0, ["maker-normal"])]


def test_a_reverted_receipt_fails_settlement_but_its_gas_is_counted():
    """A match that reverted at inclusion moved nothing, so settlement fails;
    it still burned the admin's gas, so the gas is booked before the raise."""
    gas_used: list[tuple[int, list[str]]] = []
    with pytest.raises(RuntimeError, match="reverted"):
        _settle([{"transactionHash": b"\x02", "status": 0, "gasUsed": 123_456}], gas_used)
    assert gas_used == [(123_456, ["maker-normal"])]


def test_each_group_is_counted_even_when_one_receipt_lacks_gas():
    _hashes, gas_used = _settle([
        {"transactionHash": b"\x03", "status": 1, "gasUsed": 80_000},
        {"transactionHash": b"\x04", "status": 1},
    ])
    assert gas_used == [(80_000, ["maker-normal"]), (0, ["maker-mint"])]


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
    gas_used: list[tuple[int, list[str]]] = []
    hashes = svc._settle_on_chain(taker, b"\x00", matches, gas_used)

    assert len(hashes) == 2 and len(chain.accepted) == 2
    assert sender.gas_state() == "paused"      # the breaker really did trip in between


def test_book_refunds_the_unused_reservation():
    svc, db, user = _setup()
    day = int(time.time()) // 86_400
    with db.write() as conn:
        TableWrite.add_sponsored_gas(conn, user.api_key, day, 500_000)     # what the reservation took
    svc._book_sponsored_gas(user, [(168_000, ["maker-key"])], 500_000)
    with db.read() as conn:
        assert TableRead.sponsored_gas_used(conn, user.api_key, day) == 168_000
        assert TableRead.sponsored_gas_used(conn, "maker-key", day) == 0


def test_book_refunds_everything_when_nothing_settled():
    svc, db, user = _setup()
    day = int(time.time()) // 86_400
    with db.write() as conn:
        TableWrite.add_sponsored_gas(conn, user.api_key, day, 500_000)
    svc._book_sponsored_gas(user, [], 500_000)
    with db.read() as conn:
        assert TableRead.sponsored_gas_used(conn, user.api_key, day) == 0


def test_house_taker_gas_is_split_over_the_non_house_makers():
    svc, db, user = _setup()
    day = int(time.time()) // 86_400
    with db.write() as conn:
        _u, _a, other_house = TableWrite.create_user(conn, email="h2@example.com", password_hash=None, handle=None)
        TableWrite.mark_user_as_bot(conn, other_house)
        TableWrite.mark_user_as_bot(conn, user.api_key)
        house = TableRead.get_user_by_userid(conn, user.user_id)
        _u3, _a3, maker = TableWrite.create_user(conn, email="m@example.com", password_hash=None, handle=None)
    assert house is not None
    svc._book_sponsored_gas(house, [(300_000, [maker, other_house]), (200_000, [maker])], 0)
    with db.read() as conn:
        assert TableRead.sponsored_gas_used(conn, maker, day) == 150_000 + 200_000
        assert TableRead.sponsored_gas_used(conn, other_house, day) == 0
        assert TableRead.sponsored_gas_used(conn, house.api_key, day) == 0


def test_book_trues_up_the_day_the_reservation_was_taken_on():
    # A placement that settles across 00:00 UTC: the reservation sits on day D
    # and the true-up must go there, not to D+1 as a negative row.
    svc, db, user = _setup()
    today = int(time.time()) // 86_400
    reserved_on = today - 1
    with db.write() as conn:
        TableWrite.add_sponsored_gas(conn, user.api_key, reserved_on, 500_000)
    svc._book_sponsored_gas(user, [(168_000, ["maker-key"])], 500_000, day=reserved_on)
    with db.read() as conn:
        assert TableRead.sponsored_gas_used(conn, user.api_key, reserved_on) == 168_000
        assert TableRead.sponsored_gas_used(conn, user.api_key, today) == 0
    assert _booked_rows(db, user.api_key) == 1                          # no row on the other day


def test_house_taker_gas_is_booked_on_the_given_day():
    svc, db, user = _setup()
    day = int(time.time()) // 86_400 - 1
    with db.write() as conn:
        TableWrite.mark_user_as_bot(conn, user.api_key)
        house = TableRead.get_user_by_userid(conn, user.user_id)
        _u, _a, maker = TableWrite.create_user(conn, email="m@example.com", password_hash=None, handle=None)
    assert house is not None
    svc._book_sponsored_gas(house, [(300_000, [maker])], 0, day=day)
    with db.read() as conn:
        assert TableRead.sponsored_gas_used(conn, maker, day) == 300_000
        assert TableRead.sponsored_gas_used(conn, maker, day + 1) == 0


def test_maker_rows_are_booked_in_key_order(monkeypatch):
    # Two placements booking the same makers in different orders would lock
    # their rows crosswise and deadlock; one order for everybody cannot.
    svc, db, user = _setup()
    with db.write() as conn:
        TableWrite.mark_user_as_bot(conn, user.api_key)
        house = TableRead.get_user_by_userid(conn, user.user_id)
        makers = [
            TableWrite.create_user(conn, email=f"order{i}@example.com", password_hash=None, handle=None)[2]
            for i in range(4)
        ]
    assert house is not None
    written: list[str] = []
    real = TableWrite.add_sponsored_gas

    def record(conn, api_key, day, gas):
        written.append(api_key)
        real(conn, api_key, day, gas)

    monkeypatch.setattr(TableWrite, "add_sponsored_gas", staticmethod(record))
    svc._book_sponsored_gas(house, [(400_000, sorted(makers, reverse=True))], 0)
    assert written == sorted(makers)


def test_a_receipt_timeout_never_refunds_below_the_reservation():
    # The transaction was broadcast and may well have mined unseen: its gas is
    # probably spent, so the estimate stands until a receipt says otherwise.
    svc, db, user = _setup()
    day = int(time.time()) // 86_400
    with db.write() as conn:
        TableWrite.add_sponsored_gas(conn, user.api_key, day, 500_000)
    svc._book_sponsored_gas(user, [(100_000, ["maker-key"])], 500_000, receipt_timed_out=True)
    with db.read() as conn:
        assert TableRead.sponsored_gas_used(conn, user.api_key, day) == 500_000   # one group's gas is no refund


def test_a_receipt_timeout_still_charges_what_exceeded_the_reservation():
    svc, db, user = _setup()
    day = int(time.time()) // 86_400
    with db.write() as conn:
        TableWrite.add_sponsored_gas(conn, user.api_key, day, 250_000)
    svc._book_sponsored_gas(user, [(300_000, ["a"]), (50_000, ["b"])], 250_000, receipt_timed_out=True)
    with db.read() as conn:
        assert TableRead.sponsored_gas_used(conn, user.api_key, day) == 350_000


def _rest_ask(db, api_key, token, price_micro, size_micro) -> str:
    """Rest a SELL straight in the table (no signature check, no chain): the
    book a taker will run into."""
    zero = "0x" + "00" * 20
    order = OrderData(
        salt=secrets.randbits(64), maker=zero, signer=zero, taker=zero, tokenId=int(token),
        makerAmount=size_micro, takerAmount=price_micro * size_micro // 1_000_000,
        expiration=0, nonce=0, feeRateBps=0, side=1, signatureType=0,
    )
    order_id = OrderService._compute_order_id(order)
    with db.write() as conn:
        OrderService(None, None, Settings())._insert_order(  # type: ignore[arg-type]
            conn, api_key=api_key, order=order, order_id=order_id, signature=b"\x00",
            price_int=OrderService._price_int(order), order_type="GTC",
        )
    return order_id


def test_a_refused_reservation_rolls_the_whole_placement_back():
    """Two placements that both passed the pre-check: the second one's
    reservation is refused inside the matching transaction, so nothing of it
    survives -- no order row, no fill, the maker's order untouched."""
    _svc, db, user = _setup()
    day = int(time.time()) // 86_400
    with db.write() as conn:
        _u, _a, maker = TableWrite.create_user(conn, email="rest@example.com", password_hash=None, handle=None)
        TableWrite.add_sponsored_gas(conn, user.api_key, day, 1_000_000)       # the day is already at the budget
    ask = _rest_ask(db, maker, YES, 500_000, 10_000_000)
    # A chain fake that can sign and passes the balance and breaker checks.
    onchain = SimpleNamespace(
        _client=SimpleNamespace(deployment=Deployment.load(Settings().deployment_path)),
        usd_balance=lambda _a: 10**15, ctf_balance=lambda _a, _t: 10**15,
        check_sponsored=lambda: None,
    )
    svc = OrderService(db, onchain, Settings(AGENTPIT_DAILY_SPONSORED_GAS_PER_ACCOUNT=1_000_000))  # type: ignore[arg-type]
    svc._check_gas_budget = lambda _user: None        # simulate the race: pre-check already passed

    req = _req(price="0.5", size="10").model_copy(update={"client_order_id": "coid-1"})
    with pytest.raises(GasBudgetExceededError) as info:
        svc.place_order(user, req)
    assert 0 < info.value.retry_after <= 86_400

    with db.read() as conn:
        rows = conn.execute("SELECT ORDER_ID, REMAINING_AMOUNT, STATUS FROM orders").fetchall()
        assert [r["ORDER_ID"] for r in rows] == [ask]                          # no taker row
        assert int(rows[0]["REMAINING_AMOUNT"]) == 10_000_000 and rows[0]["STATUS"] == "live"
        assert conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()["n"] == 0
        assert TableRead.get_idempotency_order_id(conn, user.api_key, "coid-1") is None
        assert TableRead.sponsored_gas_used(conn, user.api_key, day) == 1_000_000   # nothing added


def test_the_reservation_is_taken_inside_the_placement():
    """The positive side of the rollback test: with room in the budget the
    reservation is made (makers x 250k) before settlement is attempted."""
    _svc, db, user = _setup()
    day = int(time.time()) // 86_400
    with db.write() as conn:
        _u, _a, maker = TableWrite.create_user(conn, email="rest@example.com", password_hash=None, handle=None)
    _rest_ask(db, maker, YES, 500_000, 10_000_000)
    reached = []

    def settle(*_a, **_k):
        with db.read() as conn:
            reached.append(TableRead.sponsored_gas_used(conn, user.api_key, day))
        raise RuntimeError("stop at settlement")

    onchain = SimpleNamespace(
        _client=SimpleNamespace(deployment=Deployment.load(Settings().deployment_path)),
        usd_balance=lambda _a: 10**15, ctf_balance=lambda _a, _t: 10**15,
        check_sponsored=lambda: None,
    )
    svc = OrderService(db, onchain, Settings(AGENTPIT_DAILY_SPONSORED_GAS_PER_ACCOUNT=1_000_000))  # type: ignore[arg-type]
    svc._settle_on_chain = settle                      # type: ignore[method-assign]
    resp = svc.place_order(user, _req(price="0.5", size="10"))
    assert not resp.success and "settlement failed" in resp.errorMsg
    assert reached == [250_000]                        # one maker, reserved before settling
    with db.read() as conn:
        # Nothing settled, so the whole reservation came back.
        assert TableRead.sponsored_gas_used(conn, user.api_key, day) == 0


def test_the_house_reserves_nothing():
    svc, db, user = _setup()
    with db.write() as conn:
        TableWrite.mark_user_as_bot(conn, user.api_key)
        bot = TableRead.get_user_by_userid(conn, user.user_id)
        assert bot is not None
        assert svc._reserve_sponsored_gas(conn, bot, 5)[0] == 0
        assert svc._reserve_sponsored_gas(conn, user, 0)[0] == 0
    assert _booked_rows(db, user.api_key) == 0


def _fake_onchain():
    """A chain fake that signs, passes the balance and breaker checks, and has
    a `matchOrders` for `_settle_on_chain` to build (the send itself is patched)."""
    exchange = SimpleNamespace(functions=SimpleNamespace(matchOrders=lambda *args: ("matchOrders", args)))
    return SimpleNamespace(
        _client=SimpleNamespace(deployment=Deployment.load(Settings().deployment_path)),
        _contracts=SimpleNamespace(exchange=exchange),
        usd_balance=lambda _a: 10**15, ctf_balance=lambda _a, _t: 10**15,
        check_sponsored=lambda: None,
    )


def _reserving_placement(email="rest@example.com"):
    """A service whose taker will fill one resting ask and so reserve 250k."""
    _svc, db, user = _setup()
    with db.write() as conn:
        _u, _a, maker = TableWrite.create_user(conn, email=email, password_hash=None, handle=None)
    _rest_ask(db, maker, YES, 500_000, 10_000_000)
    svc = OrderService(db, _fake_onchain(), Settings(AGENTPIT_DAILY_SPONSORED_GAS_PER_ACCOUNT=1_000_000))  # type: ignore[arg-type]
    return svc, db, user


def test_a_receipt_timeout_keeps_the_reservation():
    svc, db, user = _reserving_placement()
    day = int(time.time()) // 86_400
    timeout = TimeExhausted("Transaction 0xabc is not in the chain after 60 seconds")
    with patch("agentpit.services.order_service.send_admin_tx", side_effect=timeout):
        resp = svc.place_order(user, _req(price="0.5", size="10"))
    assert not resp.success and "settlement failed" in resp.errorMsg
    with db.read() as conn:
        assert TableRead.sponsored_gas_used(conn, user.api_key, day) == 250_000   # the match may have mined


@pytest.mark.parametrize("refusal", [TxDropped("nonce taken"), RuntimeError("estimate failed")])
def test_a_failure_that_cannot_have_spent_gas_still_refunds(refusal):
    svc, db, user = _reserving_placement()
    day = int(time.time()) // 86_400
    with patch("agentpit.services.order_service.send_admin_tx", side_effect=refusal):
        resp = svc.place_order(user, _req(price="0.5", size="10"))
    assert not resp.success
    with db.read() as conn:
        assert TableRead.sponsored_gas_used(conn, user.api_key, day) == 0


def test_a_placement_settling_after_midnight_trues_up_the_day_it_reserved(monkeypatch):
    svc, db, user = _reserving_placement()
    day = int(time.time()) // 86_400 + 1
    clock = [(day + 1) * 86_400 - 5]                 # five seconds before 00:00 UTC
    monkeypatch.setattr(time, "time", lambda: clock[0])

    def settle(_order, _signature, matches, gas_used):
        clock[0] += 10                               # the fills land after midnight
        gas_used.append((168_000, [m["maker_row"]["API_KEY"] for m in matches]))
        return [b"\x01"]

    svc._settle_on_chain = settle                    # type: ignore[method-assign]
    assert svc.place_order(user, _req(price="0.5", size="10")).success
    assert clock[0] // 86_400 == day + 1
    with db.read() as conn:
        assert TableRead.sponsored_gas_used(conn, user.api_key, day) == 168_000   # 250k reserved, 82k back
        assert TableRead.sponsored_gas_used(conn, user.api_key, day + 1) == 0
    assert _booked_rows(db, user.api_key) == 1
