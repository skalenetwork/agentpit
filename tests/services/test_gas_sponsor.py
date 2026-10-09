"""`UserGasSponsor` against a fake chain and the real test database.

`_Chain` is just enough `OnchainAdmin` for the sponsor; the budget rows are real
`sponsored_gas` rows, because the reservation's refusal is one SQL statement.
Numbers: price 1,000 wei, an estimate of 100,000 gas (a limit of 120,000 after
the 20% pad, a need of 120,000,000 wei), 80,000 gas used per mined call.
"""

import functools
import logging
import secrets
import time
from collections import Counter
from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import psycopg
import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient
from urllib3.exceptions import MaxRetryError, NewConnectionError
from web3 import Web3
from web3.exceptions import TimeExhausted, Web3RPCError

from agentpit.api.exception_handlers import register_exception_handlers
from agentpit.config import Settings
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import (
    AdminGasPausedError,
    GasBudgetExceededError,
    GasPriceMovedError,
    GasTopUpTimeoutError,
    InsufficientGasError,
    NothingToClaimError,
    TransactionInProgressError,
    TransactionRevertedError,
)
from agentpit.onchain.admin import OnchainAdmin
from agentpit.onchain.tx_sender import TRANSFER_GAS, TxDropped
from agentpit.services.gas_sponsor import UserGasSponsor
from tests.db_helpers import fresh_test_db

NEED = 120_000_000  # 100,000 gas estimated, +20%, at 1,000 wei
MINED = TRANSFER_GAS + 80_000  # one top-up and one mined call
RESERVED = 120_000 + TRANSFER_GAS  # what a split holds while it sends
BUDGET = 20_000_000
SEND = ("redeem", 120_000, 1_000)  # a call as `_Chain.sends` records it
BUSY = "the platform is busy — try again in a moment"

# What the nodes answer, as web3 raises it for a single send.
SKALED_BALANCE_LOW = "Account balance is too low (balance < value + gas * gas price)"
ANVIL_BALANCE_LOW = "Insufficient funds for gas * price + value"
SKALED_FEE_LOW = "Transaction gas price lower than current eth_gasPrice"


def _refused(message: str) -> Web3RPCError:
    return Web3RPCError(repr({"code": -32000, "message": message}))


def _never_connected() -> requests.ConnectionError:
    """The connect itself was refused: the request never reached the node."""
    reason = NewConnectionError(None, "Failed to establish a new connection")
    return requests.ConnectionError(MaxRetryError(None, "/", reason))


class _Call:
    """A contract call: `estimate_gas` records the fields it is given, and the
    fake chain mines it with `status` at 80,000 gas."""

    def __init__(self, name="redeem", estimate=100_000, *, status=1):
        self.name, self.estimate, self.status = name, estimate, status
        self.estimated_with: list[dict] = []

    def estimate_gas(self, tx: dict) -> int:
        self.estimated_with.append(tx)
        return self.estimate


class _Chain:
    """Just enough `OnchainAdmin` for `UserGasSponsor`. `prices` and `balances`
    answer successive reads (the last repeats); `refusals` and `fund_errors` are
    raised by the next sends / top-ups in turn (a top-up's after its transfer is
    recorded as sent), None letting one mine; `paused` makes `fund_gas` raise
    `AdminGasPausedError`. Every send is signed first: its hash goes to `signed`
    and `on_signed` before it can be refused. There is deliberately no
    `check_sponsored`: a wallet needing no top-up must not meet the breaker.

    `fault_at=(kind, n)` makes the n-th (from 1) chain call of kind "price",
    "estimate", "balance", "fund" or "send" raise `fault`: a send fails once
    signed (as `send_user_tx` signs before the broadcast), a top-up before it is
    sent. Then `after` lists every later chain call and `mined` counts the sends
    that came back with a receipt."""

    def __init__(
        self, *, prices=(1_000,), balances=(0,), refusals=(), paused=False,
        during_send=None, fund_errors=(), fault_at=None, fault=None,
    ):  # fmt: skip
        self.prices, self.balances = list(prices), list(balances)
        self.refusals, self.fund_errors = list(refusals), list(fund_errors)
        self.paused, self.during_send = paused, during_send
        self.fault_at, self.fault = fault_at, fault
        self.events: list[tuple] = []
        self.signed: list[str] = []
        self.counts: Counter[str] = Counter()
        self.fired, self.mined = False, 0
        self.after: list[str] = []

    def _tick(self, name: str) -> None:
        if self.fired:
            self.after.append(name)
        self.counts[name] += 1
        if (name, self.counts[name]) == self.fault_at:
            self.fired = True
            raise self.fault

    @staticmethod
    def _next(values: list[int]) -> int:
        return values.pop(0) if len(values) > 1 else values[0]

    def gas_price(self) -> int:
        self._tick("price")
        return self._next(self.prices)

    def estimate_user_gas(self, fn, address: str) -> int:
        self._tick("estimate")  # then the real one: what it hands `estimate_gas`
        return OnchainAdmin.estimate_user_gas(self, fn, address)  # type: ignore[arg-type]

    def native_balance(self, address: str) -> int:
        self._tick("balance")
        self.events.append(("balance",))
        return self._next(self.balances)

    def fund_gas(self, address: str, value_wei: int, *, timeout: int = 30):
        self._tick("fund")
        if self.paused:
            raise AdminGasPausedError()
        self.events.append(("fund", value_wei))
        if self.fund_errors and (error := self.fund_errors.pop(0)) is not None:
            raise error
        return {"status": 1, "gasUsed": TRANSFER_GAS}

    def send_as_user(self, _account, fn, *, gas, max_fee, on_signed=None, **_):
        self.events.append(("send", fn.name, gas, max_fee))
        self.signed.append("0x%064x" % (len(self.signed) + 1))
        if on_signed is not None:
            on_signed(self.signed[-1])
        self._tick("send")
        if self.during_send is not None:
            self.during_send()
        if self.refusals and (refusal := self.refusals.pop(0)) is not None:
            raise refusal
        self.mined += 1
        return {"status": fn.status, "gasUsed": 80_000, "transactionHash": b"\x01" * 32}

    @property
    def funded(self) -> list[int]:
        return [e[1] for e in self.events if e[0] == "fund"]

    @property
    def sends(self) -> list[tuple]:
        return [e[1:] for e in self.events if e[0] == "send"]


def _refused_once(message=SKALED_FEE_LOW, *, calls=1, **kwargs) -> _Chain:
    """The first send is refused as `message`; the re-sizing then sees a price
    of 1,500 and a wallet that holds exactly the first top-up."""
    refusals = [_refused(message)]
    balances = (0, calls * NEED)
    return _Chain(prices=(1_000, 1_500), balances=balances, refusals=refusals, **kwargs)


def _settings(**overrides) -> Settings:
    """Every value these tests depend on, explicitly: a developer's .env must
    not move a ceiling or a budget under them."""
    return Settings(
        **{
            "AGENTPIT_SPONSOR_USER_GAS": True,
            "AGENTPIT_MAX_TOPUP_GAS": 1_000_000,
            "AGENTPIT_MIN_CLAIM_MICRO": 10_000,
            "AGENTPIT_DAILY_SPONSORED_GAS_PER_ACCOUNT": BUDGET,
            "AGENTPIT_TX_TIMEOUT_S": 30,
            **overrides,
        }
    )


def _sponsor(db, chain=None, **settings) -> UserGasSponsor:
    return UserGasSponsor(db, chain or _Chain(), _settings(**settings))  # type: ignore[arg-type]


def _user(db, *, bot: bool = False):
    with db.write() as conn:
        user_id, _acct, api_key = TableWrite.create_user(
            conn,
            email=f"sponsor-{secrets.token_hex(4)}@example.com",
            password_hash=None,
            handle=None,
        )
        if bot:
            TableWrite.mark_user_as_bot(conn, api_key)
        user = TableRead.get_user_by_userid(conn, user_id)
    assert user is not None
    return user


def _today() -> int:
    return int(time.time()) // 86_400


def _used(db, user) -> int:
    with db.read() as conn:
        return TableRead.sponsored_gas_used(conn, user.api_key, _today())


def _spend(db, user, gas: int) -> None:
    with db.write() as conn:
        TableWrite.add_sponsored_gas(conn, user.api_key, _today(), gas)


def _send(
    db, user, chain, kind="claim", calls=None, *, raises=None, match=None,
    on_signed=None, before_send=None, **settings,
):  # fmt: skip
    """Send `calls` (one redeem by default) under the user's lock. With `raises`,
    expect that error and return it, else return the receipts."""
    sponsor = _sponsor(db, chain, **settings)
    expected = pytest.raises(raises, match=match) if raises else nullcontext()
    with expected as caught, sponsor.locked(user):
        receipts = sponsor.send(  # type: ignore[arg-type]
            user, calls or [_Call()], kind, on_signed=on_signed, before_send=before_send
        )
    return caught.value if raises else receipts


@pytest.fixture
def db():
    return fresh_test_db()


@pytest.fixture
def user(db):
    return _user(db)


@pytest.fixture
def send(db, user):
    return functools.partial(_send, db, user)


@pytest.fixture
def used(db, user):
    """The gas booked today for the user (or another one)."""
    return lambda who=user: _used(db, who)


# --- sizing and the one resize-and-retry ------------------------------------
def test_min_claim_micro_comes_from_settings(db):
    assert _sponsor(db).min_claim_micro == 10_000
    assert _sponsor(db, AGENTPIT_MIN_CLAIM_MICRO=25_000).min_claim_micro == 25_000


def test_top_up_is_need_minus_balance_and_mines_before_the_send(send):
    chain = _Chain(balances=(5_000_000,))
    receipts = send(chain)
    assert chain.events == [("balance",), ("fund", NEED - 5_000_000), ("send", *SEND)]
    assert [r["status"] for r in receipts] == [1]


def test_a_covered_wallet_gets_no_top_up_and_never_meets_the_breaker(send, used):
    chain = _Chain(balances=(NEED,), paused=True)  # the breaker guards top-ups only
    send(chain)
    assert chain.events == [("balance",), ("send", *SEND)]  # no fund, no breaker
    assert used() == 80_000  # booked without a transfer


def test_one_top_up_covers_every_call(send):
    # Onboarding's three approvals, at the gas the anvil probe measured.
    chain = _Chain()
    calls = [_Call("approve_exchange", 46_487), _Call("approve_ctf", 46_487)]
    send(chain, "onboarding", [*calls, _Call("approve_all", 45_996)])
    assert chain.funded == [(55_784 + 55_784 + 55_195) * 1_000]
    assert chain.sends == [
        ("approve_exchange", 55_784, 1_000),
        ("approve_ctf", 55_784, 1_000),
        ("approve_all", 55_195, 1_000),
    ]


@pytest.mark.parametrize("kind", ["claim", "split"])
def test_the_top_up_ceiling_is_inclusive(send, used, kind, caplog):
    chain = _Chain()
    send(chain, kind, raises=RuntimeError, match="ceiling", AGENTPIT_MAX_TOPUP_GAS=100_000)
    assert chain.funded == [] and chain.sends == []
    assert used() == 0  # sized before the budget is touched
    assert any(
        r.levelno == logging.ERROR and r.name == "agentpit.services.gas_sponsor"
        for r in caplog.records
    )
    send(chain, kind, AGENTPIT_MAX_TOPUP_GAS=120_000)
    assert chain.funded == [NEED]


def test_estimates_carry_no_fee_fields(send, user):
    # With a gasPrice or maxFeePerGas anvil refuses to estimate for a dry wallet.
    call = _Call()
    send(_Chain(), calls=[call])
    assert call.estimated_with == [{"from": Web3.to_checksum_address(user.eth_address)}]


def test_fee_too_low_is_retried_once_at_the_new_price(send, used):
    chain = _refused_once()
    send(chain)
    # The second top-up is the new need (120,000 gas at 1,500) minus the first.
    assert chain.funded == [NEED, 60_000_000]
    assert chain.sends == [SEND, ("redeem", 120_000, 1_500)]
    assert used() == 2 * TRANSFER_GAS + 80_000  # both transfers, the one receipt


@pytest.mark.parametrize("message", [SKALED_BALANCE_LOW, ANVIL_BALANCE_LOW])
def test_balance_too_low_is_retried_once_with_a_fresh_top_up(send, message):
    # The first read saw enough (a stale balance), the node disagreed.
    chain = _Chain(balances=(NEED, 0), refusals=[_refused(message)])
    send(chain)
    assert chain.funded == [NEED]
    assert len(chain.sends) == 2


def test_only_the_calls_not_yet_sent_are_resized(send):
    calls = [_Call("a", 46_487), _Call("b", 46_487), _Call("c", 45_996)]
    chain = _Chain(
        prices=(1_000, 2_000),
        balances=(0, 100_000_000),
        refusals=[None, _refused(SKALED_FEE_LOW)],
    )
    send(chain, "onboarding", calls)
    assert chain.funded == [166_763_000, (55_784 + 55_195) * 2_000 - 100_000_000]
    assert chain.sends == [
        ("a", 55_784, 1_000),
        ("b", 55_784, 1_000),
        ("b", 55_784, 2_000),
        ("c", 55_195, 2_000),
    ]
    assert [len(c.estimated_with) for c in calls] == [1, 2, 2]


@pytest.mark.parametrize("message", [SKALED_BALANCE_LOW, ANVIL_BALANCE_LOW])
def test_a_second_balance_refusal_is_402(send, used, message):
    chain = _Chain(refusals=[_refused(message)] * 2)
    error = send(chain, raises=InsufficientGasError)
    assert isinstance(error.__cause__, Web3RPCError)
    assert len(chain.sends) == 2
    assert used() == 2 * TRANSFER_GAS  # both top-ups were paid


@pytest.mark.parametrize("kind", ["claim", "split"])
def test_a_second_fee_refusal_is_a_retryable_503(send, used, kind):
    # Both signatures were refused at import, so nothing is in flight.
    chain = _Chain(refusals=[_refused(SKALED_FEE_LOW)] * 2)
    error = send(chain, kind, raises=GasPriceMovedError)
    assert isinstance(error.__cause__, Web3RPCError)
    assert str(error) == "the network fee rose while sending — try again in a moment"
    assert len(chain.sends) == 2
    assert used() == 2 * TRANSFER_GAS  # both top-ups mined, so both stay booked


def test_each_call_gets_its_own_resize_and_retry(send, used):
    # One retry for the whole batch aborted onboarding after two approvals mined.
    chain = _Chain(
        prices=(1_000, 1_500, 2_000),
        balances=(0, 240_000_000, 120_000_000),
        refusals=[None, _refused(SKALED_FEE_LOW), None, _refused(SKALED_FEE_LOW)],
    )
    receipts = send(chain, "onboarding", [_Call("a"), _Call("b"), _Call("c")])
    assert [r["status"] for r in receipts] == [1, 1, 1]
    assert chain.sends == [
        ("a", 120_000, 1_000),
        ("b", 120_000, 1_000),
        ("b", 120_000, 1_500),
        ("c", 120_000, 1_500),
        ("c", 120_000, 2_000),
    ]
    assert chain.funded == [
        360_000_000,
        2 * 120_000 * 1_500 - 240_000_000,
        120_000 * 2_000 - 120_000_000,
    ]
    assert used() == 3 * TRANSFER_GAS + 3 * 80_000


def test_any_other_refusal_propagates_without_a_retry(send):
    chain = _Chain(refusals=[_refused("nonce too low")])
    send(chain, raises=Web3RPCError, match="nonce too low")
    assert chain.funded == [NEED]
    assert len(chain.sends) == 1


# --- the signing hook, the last check before it, and the per-user lock ------
def test_each_call_reports_its_index_and_hash_as_it_is_signed(send):
    chain, reported = _Chain(), []
    calls = [_Call("a"), _Call("b"), _Call("c")]
    send(chain, "onboarding", calls, on_signed=lambda *args: reported.append(args))
    assert reported == list(enumerate(chain.signed))
    assert len(set(chain.signed)) == 3


@pytest.mark.parametrize("message", [SKALED_FEE_LOW, SKALED_BALANCE_LOW])
def test_a_resized_retry_reports_its_new_hash_for_the_same_call(send, message):
    # A different transaction, so a different hash, under the same index.
    chain, reported = _refused_once(message), []
    send(chain, on_signed=lambda *args: reported.append(args))
    assert reported == [(0, chain.signed[0]), (0, chain.signed[1])]
    assert chain.signed[0] != chain.signed[1]


def test_both_hooks_run_with_the_kill_switch_off_too(send):
    chain, reported = _Chain(), []
    send(
        chain,
        "split",
        on_signed=lambda *args: reported.append(args),
        before_send=lambda: chain.events.append(("hook",)),
        AGENTPIT_SPONSOR_USER_GAS=False,
    )
    assert reported == [(0, chain.signed[0])]
    assert chain.events == [("hook",), ("send", *SEND)]


@pytest.mark.parametrize(
    ("chain", "events"),
    [
        pytest.param(
            _Chain(),
            [("balance",), ("fund", NEED), ("hook",), ("send", *SEND)],
            id="after-a-top-up",
        ),
        pytest.param(
            _Chain(balances=(NEED,)),
            [("balance",), ("hook",), ("send", *SEND)],
            id="no-top-up-needed",
        ),
        # Asked once: it guards the first signature, the retry is the same call.
        pytest.param(
            _refused_once(),
            [("balance",), ("fund", NEED), ("hook",), ("send", *SEND)]
            + [("balance",), ("fund", 60_000_000), ("send", "redeem", 120_000, 1_500)],
            id="not-again-for-a-resized-retry",
        ),
    ],
)
def test_before_send_runs_after_the_top_up_before_signing(send, chain, events):
    # A market can resolve while the top-up mines: a stale call costs only that.
    receipts = send(chain, before_send=lambda: chain.events.append(("hook",)))
    assert chain.events == events
    assert [r["status"] for r in receipts] == [1]


@pytest.mark.parametrize("kind", ["claim", "split"])
@pytest.mark.parametrize(
    ("balance", "booked", "settings"),
    [
        pytest.param(0, TRANSFER_GAS, {}, id="the-top-up-went-out"),
        pytest.param(NEED, 0, {}, id="no-top-up-was-needed"),
        pytest.param(0, 0, {"AGENTPIT_SPONSOR_USER_GAS": False}, id="kill-switch-off"),
    ],
)
def test_a_failing_before_send(db, user, send, used, kind, balance, booked, settings):
    # Nothing is signed: a split's reservation goes back, a top-up's transfer stays.
    chain, error = _Chain(balances=(balance,)), NothingToClaimError()
    refuse = Mock(side_effect=error)
    refused = send(chain, kind, before_send=refuse, raises=NothingToClaimError, **settings)
    assert refused is error
    assert chain.sends == [] and chain.signed == []
    assert used() == booked
    with _sponsor(db, chain).locked(user):  # the lock is free
        pass


def test_a_held_lock_refuses_another_request(db, user):
    # Each request builds its own sponsor on its own thread; the lock is shared.
    def other_request():
        with _sponsor(db).locked(user):
            pass

    with _sponsor(db).locked(user), ThreadPoolExecutor(1) as pool:
        with pytest.raises(TransactionInProgressError) as caught:
            pool.submit(other_request).result()
    assert str(caught.value) == (
        "another transaction for this account is in progress — try again in a moment"
    )


def test_the_lock_is_per_address_and_ignores_case(db, user):
    other, sponsor = _user(db), _sponsor(db)
    # Stored checksummed (mixed case); a lowercase spelling is the same account.
    assert user.eth_address != user.eth_address.lower()
    lowercased = user.model_copy(update={"eth_address": user.eth_address.lower()})
    with sponsor.locked(user):
        with pytest.raises(TransactionInProgressError):
            with sponsor.locked(lowercased):
                pass
        with sponsor.locked(other):  # another account is not held up
            pass


def test_the_lock_is_released_when_the_body_raises(db, user):
    sponsor = _sponsor(db)
    with pytest.raises(ValueError):
        with sponsor.locked(user):
            raise ValueError("boom")
    with sponsor.locked(user):
        pass


def test_sending_outside_the_lock_is_a_bug(db, user):
    chain = _Chain()
    with pytest.raises(RuntimeError, match="locked"):
        _sponsor(db, chain).send(user, [_Call()], "claim")  # type: ignore[list-item]
    assert chain.events == []


# --- the breaker, the daily budget and the kill switch ----------------------
@pytest.mark.parametrize("kind", ["claim", "split"])
def test_a_paused_breaker_with_a_top_up_needed_is_503(send, used, kind):
    chain = _Chain(paused=True)
    send(chain, kind, raises=AdminGasPausedError)
    assert chain.sends == []
    assert used() == 0  # a split's reservation is handed back


@pytest.mark.parametrize("kind", ["split", "merge"])
def test_split_and_merge_over_the_budget_are_429(db, user, send, used, kind):
    _spend(db, user, BUDGET)
    chain = _Chain()
    error = send(chain, kind, raises=GasBudgetExceededError)
    assert 0 < error.retry_after <= 86_400
    assert chain.funded == [] and chain.sends == []
    assert used() == BUDGET


@pytest.mark.parametrize("kind", ["claim", "onboarding"])
def test_claims_and_onboarding_are_booked_not_refused(db, user, send, used, kind):
    _spend(db, user, BUDGET)
    send(_Chain(), kind)
    assert used() == BUDGET + MINED


@pytest.mark.parametrize("kind", ["claim", "split", "merge", "onboarding"])
def test_the_house_is_never_refused_or_booked(db, used, kind):
    house = _user(db, bot=True)
    _spend(db, house, BUDGET)
    chain = _Chain()
    _send(db, house, chain, kind)
    assert chain.funded == [NEED]
    assert used(house) == BUDGET


@pytest.mark.parametrize("kind", ["split", "merge"])
def test_the_reservation_is_trued_up_to_actual_gas(db, user, send, used, kind):
    held: list[int] = []
    send(_Chain(during_send=lambda: held.append(_used(db, user))), kind)
    assert held == [RESERVED]  # the limit plus a transfer, while sending
    assert used() == MINED


@pytest.mark.parametrize("kind", ["claim", "split", "merge"])
def test_kill_switch_off_sends_without_a_top_up_or_a_booking(db, user, send, used, kind):
    _spend(db, user, BUDGET)  # an exhausted budget is not consulted either
    chain = _Chain()
    send(chain, kind, AGENTPIT_SPONSOR_USER_GAS=False)
    assert chain.events == [("send", *SEND)]  # no balance read, no top-up
    assert used() == BUDGET


def test_kill_switch_off_and_a_dry_wallet_is_402_without_a_retry(send):
    chain = _Chain(refusals=[_refused(ANVIL_BALANCE_LOW)])
    send(chain, raises=InsufficientGasError, AGENTPIT_SPONSOR_USER_GAS=False)
    assert len(chain.sends) == 1


def test_kill_switch_never_stops_onboarding(send, used):
    chain = _Chain()
    send(chain, "onboarding", AGENTPIT_SPONSOR_USER_GAS=False)
    assert chain.funded == [NEED]
    assert used() == MINED


# --- failures around the send: the reservation and the lock -----------------
@pytest.mark.parametrize(
    ("error", "booked"),
    [
        # The transaction may be out and mine, so a split's reservation stands.
        pytest.param(TimeExhausted("no receipt in 30s"), RESERVED, id="receipt-timeout"),
        pytest.param(requests.ConnectionError("reset"), RESERVED, id="transport-error"),
        # Neither of those nor a refusal at import: the receipt poll's JSON-RPC error.
        pytest.param(Web3RPCError("rate limit exceeded"), RESERVED, id="receipt-poll"),
        pytest.param(KeyboardInterrupt(), RESERVED, id="interrupt"),
        # Provably never ran, so the reservation goes back (kept, a connect failure
        # stranded the day's whole limit on every attempt during an RPC outage).
        pytest.param(_never_connected(), TRANSFER_GAS, id="never-reached"),
        pytest.param(_refused("nonce too low"), TRANSFER_GAS, id="definite-refusal"),
    ],
)
def test_what_a_failed_send_does_to_the_reservation(db, user, send, used, error, booked):
    send(_Chain(refusals=[error]), "split", raises=type(error))  # propagates as it is
    assert used() == booked  # a top-up's transfer did go out, so it stays booked
    with _sponsor(db).locked(user):  # and the lock is free
        pass


@pytest.mark.parametrize(("kind", "standing"), [("claim", 0), ("split", RESERVED)])
def test_a_top_up_receipt_timeout_is_not_paid_twice(send, used, kind, standing):
    # `fund_gas` gave up waiting (a 503, not a bare `TimeExhausted`). The top-up may
    # still mine, so a split's reservation stands (an over-count, the safe side).
    chain = _Chain(balances=(0, NEED), fund_errors=[TimeExhausted("no receipt in 30s")])
    error = send(chain, kind, raises=GasTopUpTimeoutError)
    assert isinstance(error.__cause__, TimeExhausted)
    assert str(error) == BUSY
    assert chain.funded == [NEED] and chain.sends == []
    assert used() == standing

    send(chain, kind)
    assert chain.funded == [NEED]  # no second top-up
    assert chain.sends == [SEND]
    assert used() == standing + 80_000  # the new reservation is trued up


@pytest.mark.parametrize("kind", ["claim", "split"])
def test_a_dropped_top_up_is_a_retryable_503(send, used, kind):
    # A gap filler took its nonce (`TxDropped`): it can never mine, so the
    # reservation goes back in full, unlike a timeout's.
    chain = _Chain(fund_errors=[TxDropped("its nonce went to a gap filler")])
    error = send(chain, kind, raises=GasTopUpTimeoutError)
    assert isinstance(error.__cause__, TxDropped)
    assert str(error) == BUSY
    assert chain.funded == [NEED] and chain.sends == []
    assert used() == 0

    send(chain, kind)  # the lock is free, and it funds again
    assert chain.funded == [NEED, NEED]
    assert used() == MINED


def test_a_dropped_retry_top_up_keeps_what_the_first_one_cost(send, used):
    # The first top-up did mine, so its transfer stays booked.
    chain = _refused_once(fund_errors=[None, TxDropped("a gap filler took its nonce")])
    send(chain, "split", raises=GasTopUpTimeoutError)
    assert len(chain.sends) == 1
    assert used() == TRANSFER_GAS


def test_a_failed_read_while_re_sizing_is_a_retryable_503(send, used):
    # Refused at import, then no answer for the new price: nothing is in flight, so
    # `PositionService` may drop the pending row and the reservation goes back.
    chain = _Chain(refusals=[_refused(SKALED_FEE_LOW)])
    chain.gas_price = Mock(side_effect=[1_000, requests.ReadTimeout("read timed out")])
    error = send(chain, "split", raises=GasTopUpTimeoutError)
    assert isinstance(error.__cause__, requests.ReadTimeout)
    assert len(chain.sends) == 1
    assert used() == TRANSFER_GAS  # only the first top-up's transfer


def test_a_booking_failure_never_fails_the_action(send, monkeypatch, caplog):
    broken = Mock(side_effect=RuntimeError("database gone"))
    monkeypatch.setattr(TableWrite, "add_sponsored_gas", broken)
    receipts = send(_Chain())
    assert [r["status"] for r in receipts] == [1]
    assert "booking sponsored gas failed" in caplog.text


@pytest.mark.parametrize("kind", ["claim", "split"])
def test_a_revert_raises_after_it_is_booked(send, used, kind):
    send(_Chain(), kind, [_Call(status=0)], raises=TransactionRevertedError, match=kind)
    assert used() == MINED  # reverted gas is still paid


# --- the failure matrix -----------------------------------------------------
# Every chain call `_send_sponsored` makes was made to fail once with each error
# in `_FAULTS`, for a claim, a split and onboarding. Whatever fails where, the
# lock is free afterwards, nothing goes out after the failure, the error is the
# documented one, and a split's reservation goes back exactly when nothing it
# paid for can still mine. Of those 360 cells the 112 in `_CELLS` failed before
# the sponsor followed these rules; the other 248 held.

_FAULTS = {
    "runtime": lambda: RuntimeError("boom"),
    "timeout": lambda: TimeExhausted("no receipt in 30s"),
    "never-connected": _never_connected,
    "read-timeout": lambda: requests.ReadTimeout("read timed out"),
    "fee-low": lambda: _refused(SKALED_FEE_LOW),
    "balance-low": lambda: _refused(SKALED_BALANCE_LOW),
    "dropped": lambda: TxDropped("its nonce went to a gap filler"),
    "db": lambda: psycopg.OperationalError("server closed the connection"),
    "interrupt": KeyboardInterrupt,
}
_RESIZE_FAULTS = [
    "runtime", "timeout", "never-connected", "read-timeout", "fee-low", "balance-low", "db",
]  # fmt: skip
_RETRY_TOP_UP_FAULTS = [
    "runtime", "never-connected", "read-timeout", "fee-low", "balance-low", "db",
]  # fmt: skip
_RE_SIZING = {"price2": _RESIZE_FAULTS, "estimate2": _RESIZE_FAULTS, "balance2": _RESIZE_FAULTS}
# {(scenario, kind): {point: faults}}; a point is the n-th chain call of its kind.
# In "retry" the node refuses the first send for its fee: "price2", "estimate2"
# (onboarding: "estimate4" to "estimate6") and "balance2" re-size it, "fund2" is
# the retry's top-up, "send2" the retry, onboarding's "send3"/"send4" the next
# two calls' first tries.
_CELLS = {
    ("straight", "split"): {
        "fund1": ["interrupt"],
        "send1": ["runtime", "never-connected", "db", "interrupt"],
    },
    ("retry", "claim"): {**_RE_SIZING, "fund2": _RETRY_TOP_UP_FAULTS, "send2": ["fee-low"]},
    ("retry", "split"): {
        **_RE_SIZING,
        "fund2": [*_RETRY_TOP_UP_FAULTS, "interrupt"],
        "send2": ["runtime", "never-connected", "fee-low", "db", "interrupt"],
    },
    ("retry", "onboarding"): {
        "price2": _RESIZE_FAULTS,
        **{f"estimate{n}": _RESIZE_FAULTS for n in (4, 5, 6)},
        "balance2": _RESIZE_FAULTS,
        "fund2": _RETRY_TOP_UP_FAULTS,
        "send2": ["fee-low"],
        **{f"send{n}": ["fee-low", "balance-low"] for n in (3, 4)},
    },
}
# The errors that answer the caller in place of the raw one, with their status.
_STATUS = {GasTopUpTimeoutError: 503, GasPriceMovedError: 503, InsufficientGasError: 402}


def _point(name: str) -> tuple[str, int]:
    kind = name.rstrip("0123456789")
    return kind, int(name[len(kind) :])


def _expected(scenario: str, point: str, fault: str) -> tuple[type | None, bool]:
    """(what `send` raises, None when it succeeds; whether something paid for
    may still mine, which keeps a split's reservation)."""
    raw = type(_FAULTS[fault]())
    name, n = _point(point)
    if name not in ("fund", "send"):  # a read re-sizing after a refusal
        assert scenario == "retry", "the only reads in `_CELLS` are the re-sizing's"
        # The refused transaction can never mine, the retry is not signed yet.
        return (raw if fault == "interrupt" else GasTopUpTimeoutError), False
    if name == "fund":
        # No answer leaves the top-up free to mine; the retry's top-up has a
        # refused signature before it, so its failure is an answer (503).
        unseen = fault in ("timeout", "never-connected", "read-timeout", "interrupt")
        if fault in ("timeout", "dropped"):
            return GasTopUpTimeoutError, unseen
        if fault == "interrupt" or scenario == "straight":
            return raw, unseen
        return GasTopUpTimeoutError, unseen
    # A send: the first try of a call, or the refused call's one retry.
    if fault in ("fee-low", "balance-low"):
        if (scenario, n) != ("retry", 2):
            return None, False  # re-sized and sent again, and it mined
        return (GasPriceMovedError if fault == "fee-low" else InsufficientGasError), False
    if fault == "dropped":
        return GasTopUpTimeoutError, False
    if fault == "never-connected":
        return raw, False
    return raw, True  # it may have reached the node: it may mine


def _status_of(exc: Exception) -> int:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/")
    def _raise():
        raise exc

    return TestClient(app, raise_server_exceptions=False).get("/").status_code


@pytest.mark.parametrize(
    ("scenario", "kind", "point", "fault"),
    [
        pytest.param(scenario, kind, point, fault, id=f"{scenario}-{kind}-{point}-{fault}")
        for (scenario, kind), points in _CELLS.items()
        for point, faults in points.items()
        for fault in faults
    ],
)
def test_every_failure_point_is_handled(db, user, used, scenario, kind, point, fault):
    calls = [_Call(f"call{i}") for i in range(3 if kind == "onboarding" else 1)]
    faulty = {"fault_at": _point(point), "fault": _FAULTS[fault]()}
    chain = (
        _refused_once(calls=len(calls), **faulty)
        if scenario == "retry"
        else _Chain(**faulty)
    )
    raised: BaseException | None = None
    try:
        _send(db, user, chain, kind, calls)
    except BaseException as exc:  # KeyboardInterrupt included
        raised = exc

    assert chain.fired, f"{point} is not where the calls are"
    with _sponsor(db, chain).locked(user):  # the lock is free
        pass
    expected, unseen = _expected(scenario, point, fault)
    assert (None if raised is None else type(raised)) is expected, repr(raised)
    if raised is not None:
        assert chain.after == []  # nothing, and no send, after the failure
        if isinstance(raised, Exception):
            assert _status_of(raised) == _STATUS.get(type(raised), 500)
    # Each mined top-up's transfer and receipt, or the reservation if one may mine.
    paid = len(chain.funded) * TRANSFER_GAS + chain.mined * 80_000
    reserved = RESERVED if kind == "split" and unseen else 0
    assert used() == max(paid, reserved)
