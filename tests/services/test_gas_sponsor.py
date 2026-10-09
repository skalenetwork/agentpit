"""`UserGasSponsor` against a fake chain and the real test database.

The chain is `_Chain`, just enough `OnchainAdmin` for the sponsor; the budget
rows are real `sponsored_gas` rows, because the reservation's refusal is one
SQL statement (`TableWrite.reserve_sponsored_gas`) and faking it would test
nothing. Numbers used throughout: price 1,000 wei, an estimate of 100,000 gas
(a limit of 120,000 after the 20% pad, so a need of 120,000,000 wei), and
80,000 gas used per mined call.
"""

import logging
import secrets
import threading
import time
from collections import Counter

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
    TransactionInProgressError,
    TransactionRevertedError,
)
from agentpit.onchain.admin import OnchainAdmin
from agentpit.onchain.tx_sender import TRANSFER_GAS, TxDropped
from agentpit.services.gas_sponsor import UserGasSponsor
from tests.db_helpers import fresh_test_db

NEED = 120_000_000  # 100,000 gas estimated, +20%, at 1,000 wei
MINED = TRANSFER_GAS + 80_000  # one top-up and one mined call

# What the nodes answer, as web3 raises it for a single send.
SKALED_BALANCE_LOW = "Account balance is too low (balance < value + gas * gas price)"
ANVIL_BALANCE_LOW = "Insufficient funds for gas * price + value"
SKALED_FEE_LOW = "Transaction gas price lower than current eth_gasPrice"


def _refused(message: str) -> Web3RPCError:
    return Web3RPCError(repr({"code": -32000, "message": message}))


class _Call:
    """A contract call as the sponsor sees it. The real
    `OnchainAdmin.estimate_user_gas` calls `estimate_gas` on it (recorded, so
    a test can see the fields it was given); the fake chain mines it with its
    `status`, at 80,000 gas."""

    def __init__(
        self, name: str = "redeem", estimate: int = 100_000, *, status: int = 1
    ):
        self.name = name
        self.estimate = estimate
        self.status = status
        self.estimated_with: list[dict] = []

    def estimate_gas(self, tx: dict) -> int:
        self.estimated_with.append(tx)
        return self.estimate


class _Chain:
    """Just enough `OnchainAdmin` for `UserGasSponsor`.

    `prices` and `balances` answer successive reads, the last one repeating, so
    a test can script what the retry's re-sizing sees. `refusals` are raised by
    the next sends in turn (None lets that send mine). `fund_gas` raises
    `AdminGasPausedError` when `paused`, as `AdminTxSender.send_value` does.
    `fund_errors` are raised by the next top-ups in turn, after the transfer is
    recorded as sent: a receipt timeout, where the transfer went out and its
    receipt did not come back (None lets that top-up mine).
    Every send is signed first: it gets a hash of its own, recorded in
    `signed` and handed to `on_signed` before the send can be refused, as
    `send_user_tx` hands it over before the broadcast.
    There is deliberately no `check_sponsored`: a wallet that needs no top-up
    must not meet the breaker at all.
    """

    # The real one: what it hands `estimate_gas` is under test.
    estimate_user_gas = OnchainAdmin.estimate_user_gas

    def __init__(
        self,
        *,
        prices=(1_000,),
        balances=(0,),
        refusals=(),
        paused=False,
        during_send=None,
        fund_errors=(),
    ):
        self.prices = list(prices)
        self.balances = list(balances)
        self.refusals = list(refusals)
        self.fund_errors = list(fund_errors)
        self.paused = paused
        self.during_send = during_send
        self.events: list[tuple] = []
        self.signed: list[str] = []

    @staticmethod
    def _next(values: list[int]) -> int:
        return values.pop(0) if len(values) > 1 else values[0]

    def gas_price(self) -> int:
        return self._next(self.prices)

    def native_balance(self, address: str) -> int:
        self.events.append(("balance",))
        return self._next(self.balances)

    def fund_gas(self, address: str, value_wei: int, *, timeout: int = 30):
        if self.paused:
            raise AdminGasPausedError()
        self.events.append(("fund", value_wei))
        error = self.fund_errors.pop(0) if self.fund_errors else None
        if error is not None:
            raise error
        return {"status": 1, "gasUsed": TRANSFER_GAS}

    def send_as_user(
        self,
        user_account,
        fn,
        *,
        gas: int,
        max_fee: int,
        timeout: int = 30,
        on_signed=None,
    ):
        self.events.append(("send", fn.name, gas, max_fee))
        tx_hash = "0x%064x" % (len(self.signed) + 1)
        self.signed.append(tx_hash)
        if on_signed is not None:
            on_signed(tx_hash)
        if self.during_send is not None:
            self.during_send()
        refusal = self.refusals.pop(0) if self.refusals else None
        if refusal is not None:
            raise refusal
        return {"status": fn.status, "gasUsed": 80_000, "transactionHash": b"\x01" * 32}

    @property
    def funded(self) -> list[int]:
        return [e[1] for e in self.events if e[0] == "fund"]

    @property
    def sends(self) -> list[tuple]:
        return [e[1:] for e in self.events if e[0] == "send"]


def _settings(**overrides) -> Settings:
    """Every value these tests depend on, explicitly: a developer's .env must
    not move a ceiling or a budget under them."""
    values: dict = {
        "AGENTPIT_SPONSOR_USER_GAS": True,
        "AGENTPIT_MAX_TOPUP_GAS": 1_000_000,
        "AGENTPIT_MIN_CLAIM_MICRO": 10_000,
        "AGENTPIT_DAILY_SPONSORED_GAS_PER_ACCOUNT": 20_000_000,
        "AGENTPIT_TX_TIMEOUT_S": 30,
    }
    values.update(overrides)
    return Settings(**values)


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


def _send(db, chain, user, calls, kind, *, on_signed=None, **settings):
    sponsor = UserGasSponsor(db, chain, _settings(**settings))  # type: ignore[arg-type]
    with sponsor.locked(user):
        return sponsor.send(user, calls, kind, on_signed=on_signed)  # type: ignore[arg-type]


# --- settings ---------------------------------------------------------------


def test_min_claim_micro_comes_from_settings():
    db = fresh_test_db()
    assert UserGasSponsor(db, _Chain(), _settings()).min_claim_micro == 10_000  # type: ignore[arg-type]
    sponsor = UserGasSponsor(db, _Chain(), _settings(AGENTPIT_MIN_CLAIM_MICRO=25_000))  # type: ignore[arg-type]
    assert sponsor.min_claim_micro == 25_000


# --- sizing and the top-up --------------------------------------------------


def test_the_top_up_is_need_minus_balance_and_mines_before_the_send():
    db = fresh_test_db()
    user = _user(db)
    chain = _Chain(balances=(5_000_000,))
    receipts = _send(db, chain, user, [_Call()], "claim")
    assert chain.events == [
        ("balance",),
        ("fund", NEED - 5_000_000),
        ("send", "redeem", 120_000, 1_000),
    ]
    assert [r["status"] for r in receipts] == [1]


def test_one_top_up_covers_every_call():
    """Onboarding's three approvals, at the gas the anvil probe measured."""
    db = fresh_test_db()
    user = _user(db)
    chain = _Chain()
    calls = [
        _Call("approve_exchange", 46_487),
        _Call("approve_ctf", 46_487),
        _Call("approve_all", 45_996),
    ]
    _send(db, chain, user, calls, "onboarding")
    assert chain.funded == [(55_784 + 55_784 + 55_195) * 1_000]
    assert chain.sends == [
        ("approve_exchange", 55_784, 1_000),
        ("approve_ctf", 55_784, 1_000),
        ("approve_all", 55_195, 1_000),
    ]


def test_a_covered_wallet_gets_no_top_up_and_never_meets_the_breaker():
    """The breaker is paused, yet a wallet that already holds the need goes
    ahead: the breaker sits in front of the top-up only. Booked without a
    transfer, since none was sent."""
    db = fresh_test_db()
    user = _user(db)
    chain = _Chain(balances=(NEED,), paused=True)
    _send(db, chain, user, [_Call()], "claim")
    assert chain.funded == []
    assert chain.sends == [("redeem", 120_000, 1_000)]
    assert _used(db, user) == 80_000


@pytest.mark.parametrize("kind", ["claim", "split"])
def test_a_top_up_over_the_ceiling_raises_before_anything_is_sent(kind, caplog):
    db = fresh_test_db()
    user = _user(db)
    chain = _Chain()
    with pytest.raises(RuntimeError, match="ceiling"):
        _send(db, chain, user, [_Call()], kind, AGENTPIT_MAX_TOPUP_GAS=100_000)
    assert chain.funded == [] and chain.sends == []
    assert _used(db, user) == 0  # sized before the budget is touched
    assert any(
        r.levelno == logging.ERROR and r.name == "agentpit.services.gas_sponsor"
        for r in caplog.records
    )


def test_a_top_up_at_the_ceiling_is_allowed():
    db = fresh_test_db()
    user = _user(db)
    chain = _Chain()
    _send(db, chain, user, [_Call()], "claim", AGENTPIT_MAX_TOPUP_GAS=120_000)
    assert chain.funded == [NEED]


def test_estimates_carry_no_fee_fields():
    """With a gasPrice or maxFeePerGas, anvil refuses to estimate for a dry
    wallet ("gas required exceeds allowance: 0")."""
    db = fresh_test_db()
    user = _user(db)
    call = _Call()
    _send(db, _Chain(), user, [call], "claim")
    assert call.estimated_with == [{"from": Web3.to_checksum_address(user.eth_address)}]


# --- one resize-and-retry ---------------------------------------------------


def test_fee_too_low_is_retried_once_at_the_new_price():
    db = fresh_test_db()
    user = _user(db)
    chain = _Chain(
        prices=(1_000, 1_500), balances=(0, NEED), refusals=[_refused(SKALED_FEE_LOW)]
    )
    _send(db, chain, user, [_Call()], "claim")
    # The second top-up is the new need (120,000 gas at 1,500) minus the first.
    assert chain.funded == [NEED, 60_000_000]
    assert chain.sends == [("redeem", 120_000, 1_000), ("redeem", 120_000, 1_500)]
    # Each top-up's transfer is booked, plus the one receipt.
    assert _used(db, user) == 2 * TRANSFER_GAS + 80_000


@pytest.mark.parametrize("message", [SKALED_BALANCE_LOW, ANVIL_BALANCE_LOW])
def test_balance_too_low_is_retried_once_with_a_fresh_top_up(message):
    """The first read saw enough (a stale balance), the node disagreed."""
    db = fresh_test_db()
    user = _user(db)
    chain = _Chain(balances=(NEED, 0), refusals=[_refused(message)])
    _send(db, chain, user, [_Call()], "claim")
    assert chain.funded == [NEED]
    assert len(chain.sends) == 2


def test_only_the_calls_not_yet_sent_are_resized():
    db = fresh_test_db()
    user = _user(db)
    first, second, third = _Call("a", 46_487), _Call("b", 46_487), _Call("c", 45_996)
    chain = _Chain(
        prices=(1_000, 2_000),
        balances=(0, 100_000_000),
        refusals=[None, _refused(SKALED_FEE_LOW)],
    )
    _send(db, chain, user, [first, second, third], "onboarding")
    assert chain.funded == [166_763_000, (55_784 + 55_195) * 2_000 - 100_000_000]
    assert chain.sends == [
        ("a", 55_784, 1_000),
        ("b", 55_784, 1_000),
        ("b", 55_784, 2_000),
        ("c", 55_195, 2_000),
    ]
    assert [len(c.estimated_with) for c in (first, second, third)] == [1, 2, 2]


@pytest.mark.parametrize("message", [SKALED_BALANCE_LOW, ANVIL_BALANCE_LOW])
def test_a_second_balance_refusal_is_402(message):
    db = fresh_test_db()
    user = _user(db)
    chain = _Chain(refusals=[_refused(message), _refused(message)])
    with pytest.raises(InsufficientGasError) as caught:
        _send(db, chain, user, [_Call()], "claim")
    assert isinstance(caught.value.__cause__, Web3RPCError)
    assert len(chain.sends) == 2
    assert _used(db, user) == 2 * TRANSFER_GAS  # both top-ups were paid


@pytest.mark.parametrize("kind", ["claim", "split"])
def test_a_second_fee_refusal_is_a_retryable_503_and_hands_the_reservation_back(kind):
    """The fee rose again between the re-sizing and the retry: the node
    refused both signatures at import, so neither can mine. Not the raw
    `Web3RPCError` (a 500) it used to propagate as: `GasPriceMovedError`
    (503), "try again". Nothing is in flight, so a split's reservation goes
    back; both top-ups mined, so their transfers stay booked."""
    db = fresh_test_db()
    user = _user(db)
    chain = _Chain(refusals=[_refused(SKALED_FEE_LOW), _refused(SKALED_FEE_LOW)])
    with pytest.raises(GasPriceMovedError) as caught:
        _send(db, chain, user, [_Call()], kind)
    assert isinstance(caught.value.__cause__, Web3RPCError)
    assert str(caught.value) == (
        "the network fee rose while sending — try again in a moment"
    )
    assert len(chain.sends) == 2
    assert _used(db, user) == 2 * TRANSFER_GAS


def test_each_call_gets_its_own_resize_and_retry():
    """Onboarding's three approvals while the fee climbs: approval 2 is
    refused at 1,000 and goes out at 1,500, approval 3 is refused at 1,500 and
    goes out at 2,000. Each call has its own one retry; with one retry for the
    whole batch the second refusal aborted onboarding after two approvals had
    mined."""
    db = fresh_test_db()
    user = _user(db)
    calls = [_Call("a"), _Call("b"), _Call("c")]
    chain = _Chain(
        prices=(1_000, 1_500, 2_000),
        balances=(0, 240_000_000, 120_000_000),
        refusals=[None, _refused(SKALED_FEE_LOW), None, _refused(SKALED_FEE_LOW)],
    )
    receipts = _send(db, chain, user, calls, "onboarding")
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
    assert _used(db, user) == 3 * TRANSFER_GAS + 3 * 80_000


def test_any_other_refusal_propagates_without_a_retry():
    db = fresh_test_db()
    user = _user(db)
    chain = _Chain(refusals=[_refused("nonce too low")])
    with pytest.raises(Web3RPCError, match="nonce too low"):
        _send(db, chain, user, [_Call()], "claim")
    assert chain.funded == [NEED]
    assert len(chain.sends) == 1


# --- the signing hook -------------------------------------------------------


def test_each_call_reports_its_index_and_hash_as_it_is_signed():
    """`on_signed(i, tx_hash)` for every call, in order: what the caller writes
    its intent row under before the broadcast."""
    db = fresh_test_db()
    user = _user(db)
    chain = _Chain()
    reported: list[tuple[int, str]] = []
    _send(
        db,
        chain,
        user,
        [_Call("a"), _Call("b"), _Call("c")],
        "onboarding",
        on_signed=lambda i, tx_hash: reported.append((i, tx_hash)),
    )
    assert reported == list(enumerate(chain.signed))
    assert len(set(chain.signed)) == 3


@pytest.mark.parametrize("message", [SKALED_FEE_LOW, SKALED_BALANCE_LOW])
def test_a_resized_retry_reports_its_new_hash_for_the_same_call(message):
    """The node refused the first signature at import; the retry is signed
    again, at the new size, and is a different transaction with a different
    hash. Reported under the same index, so the caller can tell that the
    first one was refused."""
    db = fresh_test_db()
    user = _user(db)
    chain = _Chain(
        prices=(1_000, 1_500), balances=(0, NEED), refusals=[_refused(message)]
    )
    reported: list[tuple[int, str]] = []
    _send(
        db,
        chain,
        user,
        [_Call()],
        "claim",
        on_signed=lambda i, tx_hash: reported.append((i, tx_hash)),
    )
    assert reported == [(0, chain.signed[0]), (0, chain.signed[1])]
    assert chain.signed[0] != chain.signed[1]


def test_the_hook_reports_with_the_kill_switch_off_too():
    db = fresh_test_db()
    user = _user(db)
    chain = _Chain()
    reported: list[tuple[int, str]] = []
    _send(
        db,
        chain,
        user,
        [_Call()],
        "split",
        on_signed=lambda i, tx_hash: reported.append((i, tx_hash)),
        AGENTPIT_SPONSOR_USER_GAS=False,
    )
    assert reported == [(0, chain.signed[0])]


def test_no_hook_is_the_default():
    db = fresh_test_db()
    user = _user(db)
    chain = _Chain()
    sponsor = UserGasSponsor(db, chain, _settings())  # type: ignore[arg-type]
    with sponsor.locked(user):
        sponsor.send(user, [_Call()], "claim")  # type: ignore[list-item]
    assert len(chain.signed) == 1


# --- the per-user lock ------------------------------------------------------


def test_a_held_lock_refuses_another_request():
    """Services are built per request, so the second request has its own
    sponsor, on its own thread: the lock must still be the same one."""
    db = fresh_test_db()
    user = _user(db)
    outcome: list[object] = []

    def other_request():
        try:
            with UserGasSponsor(db, _Chain(), _settings()).locked(user):  # type: ignore[arg-type]
                outcome.append("entered")
        except TransactionInProgressError as exc:
            outcome.append(exc)

    with UserGasSponsor(db, _Chain(), _settings()).locked(user):  # type: ignore[arg-type]
        worker = threading.Thread(target=other_request)
        worker.start()
        worker.join()
    assert len(outcome) == 1 and isinstance(outcome[0], TransactionInProgressError)
    assert (
        str(outcome[0])
        == "another transaction for this account is in progress — try again in a moment"
    )


def test_the_lock_is_per_address_and_ignores_case():
    db = fresh_test_db()
    user, other = _user(db), _user(db)
    sponsor = UserGasSponsor(db, _Chain(), _settings())  # type: ignore[arg-type]
    # Stored checksummed (mixed case); a lowercase spelling is the same account.
    assert user.eth_address != user.eth_address.lower()
    lowercased = user.model_copy(update={"eth_address": user.eth_address.lower()})
    with sponsor.locked(user):
        with pytest.raises(TransactionInProgressError):
            with sponsor.locked(lowercased):
                pass
        with sponsor.locked(other):  # another account is not held up
            pass


def test_the_lock_is_released_when_the_body_raises():
    db = fresh_test_db()
    user = _user(db)
    sponsor = UserGasSponsor(db, _Chain(), _settings())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        with sponsor.locked(user):
            raise ValueError("boom")
    with sponsor.locked(user):
        pass


def test_sending_outside_the_lock_is_a_bug():
    db = fresh_test_db()
    user = _user(db)
    chain = _Chain()
    sponsor = UserGasSponsor(db, chain, _settings())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="locked"):
        sponsor.send(user, [_Call()], "claim")  # type: ignore[list-item]
    assert chain.events == []


# --- the breaker ------------------------------------------------------------


@pytest.mark.parametrize("kind", ["claim", "split"])
def test_a_paused_breaker_with_a_top_up_needed_is_503(kind):
    db = fresh_test_db()
    user = _user(db)
    chain = _Chain(paused=True)
    with pytest.raises(AdminGasPausedError):
        _send(db, chain, user, [_Call()], kind)
    assert chain.sends == []
    assert _used(db, user) == 0  # a split's reservation is handed back


# --- the daily budget -------------------------------------------------------


@pytest.mark.parametrize("kind", ["split", "merge"])
def test_split_and_merge_over_the_budget_are_429(kind):
    db = fresh_test_db()
    user = _user(db)
    _spend(db, user, 20_000_000)
    chain = _Chain()
    with pytest.raises(GasBudgetExceededError) as caught:
        _send(db, chain, user, [_Call()], kind)
    assert 0 < caught.value.retry_after <= 86_400
    assert chain.funded == [] and chain.sends == []
    assert _used(db, user) == 20_000_000


@pytest.mark.parametrize("kind", ["claim", "onboarding"])
def test_claims_and_onboarding_are_never_refused_but_are_booked(kind):
    db = fresh_test_db()
    user = _user(db)
    _spend(db, user, 20_000_000)
    _send(db, _Chain(), user, [_Call()], kind)
    assert _used(db, user) == 20_000_000 + MINED


@pytest.mark.parametrize("kind", ["claim", "split", "merge", "onboarding"])
def test_the_house_is_never_refused_or_booked(kind):
    db = fresh_test_db()
    house = _user(db, bot=True)
    _spend(db, house, 20_000_000)
    chain = _Chain()
    _send(db, chain, house, [_Call()], kind)
    assert chain.funded == [NEED]
    assert _used(db, house) == 20_000_000


@pytest.mark.parametrize("kind", ["split", "merge"])
def test_the_reservation_is_trued_up_to_actual_gas(kind):
    db = fresh_test_db()
    user = _user(db)
    held: list[int] = []
    chain = _Chain(during_send=lambda: held.append(_used(db, user)))
    _send(db, chain, user, [_Call()], kind)
    assert held == [120_000 + TRANSFER_GAS]  # the limit plus a transfer, while sending
    assert _used(db, user) == MINED


def test_a_receipt_timeout_keeps_the_reservation():
    """The split may still mine, so nothing is refunded."""
    db = fresh_test_db()
    user = _user(db)
    chain = _Chain(refusals=[TimeExhausted("no receipt in 30s")])
    with pytest.raises(TimeExhausted):
        _send(db, chain, user, [_Call()], "split")
    assert _used(db, user) == 120_000 + TRANSFER_GAS


@pytest.mark.parametrize(
    ("kind", "standing"), [("claim", 0), ("split", 120_000 + TRANSFER_GAS)]
)
def test_a_top_up_whose_receipt_times_out_sends_nothing_and_is_not_paid_twice(
    kind, standing
):
    """`fund_gas` gave up waiting for the top-up's receipt (or found no free
    admin transaction slot). No user transaction goes out and the caller gets
    `GasTopUpTimeoutError` (503) instead of a bare `TimeExhausted` (500). The
    top-up may still mine, so a split's
    reservation stands (an over-count, the safe direction) and nothing else is
    booked. Once the late top-up has mined, the next send sizes against the
    balance it left and tops up only max(0, need - balance): nothing here."""
    db = fresh_test_db()
    user = _user(db)
    # The first read sees the empty wallet; every later one sees the late top-up.
    chain = _Chain(balances=(0, NEED), fund_errors=[TimeExhausted("no receipt in 30s")])
    with pytest.raises(GasTopUpTimeoutError) as caught:
        _send(db, chain, user, [_Call()], kind)
    assert isinstance(caught.value.__cause__, TimeExhausted)
    assert str(caught.value) == "the platform is busy — try again in a moment"
    assert chain.funded == [NEED] and chain.sends == []
    assert _used(db, user) == standing

    _send(db, chain, user, [_Call()], kind)
    assert chain.funded == [NEED]  # no second top-up
    assert chain.sends == [("redeem", 120_000, 1_000)]
    assert (
        _used(db, user) == standing + 80_000
    )  # the new reservation is trued up; the old one stands


@pytest.mark.parametrize("kind", ["claim", "split"])
def test_a_dropped_top_up_is_a_retryable_503_and_hands_the_reservation_back(kind):
    """The node lost the top-up and a gap filler took its nonce (`TxDropped`):
    it can never mine. That is the same "busy, try again" answer as a timeout,
    not a bare `RuntimeError` (a 500). Unlike a timeout, nothing may mine
    later, so a split's reservation goes back in full and the retry tops up
    again."""
    db = fresh_test_db()
    user = _user(db)
    chain = _Chain(fund_errors=[TxDropped("its nonce went to a gap filler")])
    with pytest.raises(GasTopUpTimeoutError) as caught:
        _send(db, chain, user, [_Call()], kind)
    assert isinstance(caught.value.__cause__, TxDropped)
    assert str(caught.value) == "the platform is busy — try again in a moment"
    assert chain.funded == [NEED] and chain.sends == []
    assert _used(db, user) == 0

    _send(db, chain, user, [_Call()], kind)  # the lock is free, and it funds again
    assert chain.funded == [NEED, NEED]
    assert _used(db, user) == TRANSFER_GAS + 80_000


def test_a_dropped_retry_top_up_keeps_what_the_first_one_cost():
    """The node refused the first send at import (the price rose) and the
    re-sizing top-up was then dropped. The first top-up did mine, so its
    transfer stays booked; the refused transaction never ran."""
    db = fresh_test_db()
    user = _user(db)
    chain = _Chain(
        prices=(1_000, 1_500),
        balances=(0, NEED),
        refusals=[_refused(SKALED_FEE_LOW)],
        fund_errors=[None, TxDropped("its nonce went to a gap filler")],
    )
    with pytest.raises(GasTopUpTimeoutError):
        _send(db, chain, user, [_Call()], "split")
    assert len(chain.sends) == 1
    assert _used(db, user) == TRANSFER_GAS


def test_a_transport_error_after_the_broadcast_keeps_the_reservation():
    """No answer to the send (a reset, a proxy's 502): the node may hold the
    transaction and mine it, so the reservation stands like a receipt
    timeout's. The error itself propagates as it is."""
    db = fresh_test_db()
    user = _user(db)
    chain = _Chain(refusals=[requests.ConnectionError("connection reset by peer")])
    with pytest.raises(requests.ConnectionError):
        _send(db, chain, user, [_Call()], "split")
    assert _used(db, user) == 120_000 + TRANSFER_GAS


def _never_connected() -> requests.ConnectionError:
    """What requests raises when the connect itself was refused: the request
    never reached the node (`chain_rpc.failed_before_connecting`)."""
    return requests.ConnectionError(
        MaxRetryError(
            None, "/", NewConnectionError(None, "Failed to establish a new connection")
        )
    )


def test_a_send_that_never_reached_the_node_hands_the_reservation_back():
    """The broadcast failed to connect at all, so the node never saw the
    split: nothing can mine, and the reservation goes back like a refusal's.
    Kept, it stranded the whole limit for the day on every attempt during an
    RPC outage. The top-up did mine, so its transfer stays booked. The error
    itself propagates as it is."""
    db = fresh_test_db()
    user = _user(db)
    chain = _Chain(refusals=[_never_connected()])
    with pytest.raises(requests.ConnectionError):
        _send(db, chain, user, [_Call()], "split")
    assert _used(db, user) == TRANSFER_GAS


def test_an_unrecognised_error_after_the_broadcast_keeps_the_reservation():
    """An error from `send_as_user` that is neither a receipt timeout, nor a
    transport error, nor a refusal at import: here a JSON-RPC error from the
    receipt poll, which runs once the node has taken the transaction. It may
    well mine, so the reservation stands, as `PositionService` keeps the
    split pending for the same error."""
    db = fresh_test_db()
    user = _user(db)
    chain = _Chain(refusals=[Web3RPCError("rate limit exceeded")])
    with pytest.raises(Web3RPCError):
        _send(db, chain, user, [_Call()], "split")
    assert _used(db, user) == 120_000 + TRANSFER_GAS


def test_an_interrupt_after_the_broadcast_keeps_the_reservation_and_frees_the_lock():
    """A `KeyboardInterrupt` (or any `BaseException`) while the split's
    transaction may already be out: it is re-raised as it is, the lock is
    free, and the reservation stands, since the split may still mine."""
    db = fresh_test_db()
    user = _user(db)
    sponsor = UserGasSponsor(db, _Chain(refusals=[KeyboardInterrupt()]), _settings())  # type: ignore[arg-type]
    with pytest.raises(KeyboardInterrupt):
        with sponsor.locked(user):
            sponsor.send(user, [_Call()], "split")  # type: ignore[list-item]
    with sponsor.locked(user):
        pass
    assert _used(db, user) == 120_000 + TRANSFER_GAS


def test_a_failed_read_while_re_sizing_is_a_retryable_503_with_nothing_in_flight():
    """The node refused the split at import (its fee was low), and the read
    of the new price then got no answer. The refused transaction can never
    mine and the retry was never signed, so nothing is in flight: the caller
    gets `GasTopUpTimeoutError` (503, "try again"), which `PositionService`
    reads as an answer and drops the pending row for, and the reservation goes
    back. Only the first top-up's transfer is booked."""
    db = fresh_test_db()
    user = _user(db)
    chain = _Chain(refusals=[_refused(SKALED_FEE_LOW)])
    prices = iter([1_000])

    def gas_price() -> int:
        price = next(prices, None)
        if price is None:
            raise requests.ReadTimeout("read timed out")
        return price

    chain.gas_price = gas_price  # type: ignore[method-assign]
    with pytest.raises(GasTopUpTimeoutError) as caught:
        _send(db, chain, user, [_Call()], "split")
    assert isinstance(caught.value.__cause__, requests.ReadTimeout)
    assert len(chain.sends) == 1
    assert _used(db, user) == TRANSFER_GAS


def test_a_definite_refusal_hands_a_split_reservation_back():
    """The counterpart: the node answered and refused ("nonce too low"), so the
    transaction provably never ran. Only the top-up's transfer, which did go
    out, stays booked."""
    db = fresh_test_db()
    user = _user(db)
    chain = _Chain(refusals=[_refused("nonce too low")])
    with pytest.raises(Web3RPCError):
        _send(db, chain, user, [_Call()], "split")
    assert _used(db, user) == TRANSFER_GAS


def test_a_booking_failure_never_fails_the_action(monkeypatch, caplog):
    db = fresh_test_db()
    user = _user(db)

    def _broken(*_a, **_k):
        raise RuntimeError("database gone")

    monkeypatch.setattr(TableWrite, "add_sponsored_gas", _broken)
    receipts = _send(db, _Chain(), user, [_Call()], "claim")
    assert [r["status"] for r in receipts] == [1]
    assert "booking sponsored gas failed" in caplog.text


# --- status -----------------------------------------------------------------


@pytest.mark.parametrize("kind", ["claim", "split"])
def test_a_revert_raises_after_it_is_booked(kind):
    db = fresh_test_db()
    user = _user(db)
    with pytest.raises(TransactionRevertedError, match=kind):
        _send(db, _Chain(), user, [_Call(status=0)], kind)
    assert _used(db, user) == MINED  # reverted gas is still paid


# --- the kill switch --------------------------------------------------------


@pytest.mark.parametrize("kind", ["claim", "split", "merge"])
def test_kill_switch_off_sends_without_a_top_up_or_a_booking(kind):
    db = fresh_test_db()
    user = _user(db)
    _spend(db, user, 20_000_000)  # an exhausted budget is not consulted either
    chain = _Chain()
    _send(db, chain, user, [_Call()], kind, AGENTPIT_SPONSOR_USER_GAS=False)
    assert chain.events == [
        ("send", "redeem", 120_000, 1_000)
    ]  # no balance read, no top-up
    assert _used(db, user) == 20_000_000


def test_kill_switch_off_and_a_dry_wallet_is_402_without_a_retry():
    db = fresh_test_db()
    user = _user(db)
    chain = _Chain(refusals=[_refused(ANVIL_BALANCE_LOW)])
    with pytest.raises(InsufficientGasError):
        _send(db, chain, user, [_Call()], "claim", AGENTPIT_SPONSOR_USER_GAS=False)
    assert len(chain.sends) == 1


def test_kill_switch_never_stops_onboarding():
    db = fresh_test_db()
    user = _user(db)
    chain = _Chain()
    _send(db, chain, user, [_Call()], "onboarding", AGENTPIT_SPONSOR_USER_GAS=False)
    assert chain.funded == [NEED]
    assert _used(db, user) == MINED


# --- the failure matrix -----------------------------------------------------
#
# Every chain call `_send_sponsored` makes was made to fail once with each kind
# of error in `_FAULTS`, for a claim, a split and onboarding: the reads that
# size it, the reservation, the top-up, each send, and after a fee refusal the
# re-sizing reads, the retry's top-up and the retry. Whatever fails where, the
# lock is free afterwards, nothing goes out after the failure, the error is the
# documented one, and a split's reservation goes back exactly when nothing it
# paid for can still mine. Of those 360 cells the 112 kept in `_CELLS` failed
# before the sponsor followed these rules; the other 248 held already.


class _FaultyChain(_Chain):
    """`_Chain` whose `at`-th chain call raises `fault`: `at` is ("price", n),
    ("estimate", n), ("balance", n), ("fund", n) or ("send", n), the n-th
    call of that kind from 1. A send fails once it is signed, as
    `send_user_tx` signs before the broadcast; a top-up fails before it is
    sent. `after` lists every chain call made once the fault fired; `mined`
    counts the sends that came back with a receipt."""

    def __init__(self, at: tuple[str, int], fault: BaseException, **kwargs):
        super().__init__(during_send=lambda: self.tick("send"), **kwargs)
        self.at = at
        self.fault = fault
        self.counts: Counter[str] = Counter()
        self.fired = False
        self.after: list[str] = []
        self.mined = 0

    def tick(self, name: str) -> None:
        if self.fired:
            self.after.append(name)
        self.counts[name] += 1
        if (name, self.counts[name]) == self.at:
            self.fired = True
            raise self.fault

    def gas_price(self) -> int:
        self.tick("price")
        return super().gas_price()

    def estimate_user_gas(self, fn, address: str) -> int:
        self.tick("estimate")
        return OnchainAdmin.estimate_user_gas(self, fn, address)  # type: ignore[arg-type]

    def native_balance(self, address: str) -> int:
        self.tick("balance")
        return super().native_balance(address)

    def fund_gas(self, address: str, value_wei: int, *, timeout: int = 30):
        self.tick("fund")
        return super().fund_gas(address, value_wei, timeout=timeout)

    def send_as_user(self, *args, **kwargs):
        receipt = super().send_as_user(*args, **kwargs)
        self.mined += 1
        return receipt


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
# {(scenario, kind): {point: faults}}. A point is the n-th chain call of its
# kind, from 1, in the order `_send_sponsored` makes them. In "retry" the node
# refuses the first send for its fee: "price2", "estimate2" (onboarding:
# "estimate4" to "estimate6") and "balance2" re-size it, "fund2" is the
# retry's top-up and "send2" the retry, and onboarding's "send3" and "send4"
# are its next two calls' first tries.
_CELLS = {
    ("straight", "split"): {
        "fund1": ["interrupt"],
        "send1": ["runtime", "never-connected", "db", "interrupt"],
    },
    ("retry", "claim"): {
        "price2": _RESIZE_FAULTS,
        "estimate2": _RESIZE_FAULTS,
        "balance2": _RESIZE_FAULTS,
        "fund2": _RETRY_TOP_UP_FAULTS,
        "send2": ["fee-low"],
    },
    ("retry", "split"): {
        "price2": _RESIZE_FAULTS,
        "estimate2": _RESIZE_FAULTS,
        "balance2": _RESIZE_FAULTS,
        "fund2": [*_RETRY_TOP_UP_FAULTS, "interrupt"],
        "send2": ["runtime", "never-connected", "fee-low", "db", "interrupt"],
    },
    ("retry", "onboarding"): {
        "price2": _RESIZE_FAULTS,
        "estimate4": _RESIZE_FAULTS,
        "estimate5": _RESIZE_FAULTS,
        "estimate6": _RESIZE_FAULTS,
        "balance2": _RESIZE_FAULTS,
        "fund2": _RETRY_TOP_UP_FAULTS,
        "send2": ["fee-low"],
        "send3": ["fee-low", "balance-low"],
        "send4": ["fee-low", "balance-low"],
    },
}


def _point(name: str) -> tuple[str, int]:
    kind = name.rstrip("0123456789")
    return kind, int(name[len(kind) :])


def _phase(scenario: str, point: str) -> str:
    """What `_send_sponsored` is doing at `point`: topping up, sending a call
    for the first time, re-sizing after a refusal, or sending the refused
    call again."""
    name, n = _point(point)
    if name == "fund":
        return "top-up"
    if name == "send":
        return "resend" if (scenario, n) == ("retry", 2) else "send"
    assert scenario == "retry", "the only reads in `_CELLS` are the re-sizing's"
    return "resize"


# The errors that answer the caller in place of the error itself, and the
# status each maps to; anything else propagates as it is (500).
_STATUS = {GasTopUpTimeoutError: 503, GasPriceMovedError: 503, InsufficientGasError: 402}


def _expected(scenario: str, point: str, fault: str) -> tuple[type | None, bool]:
    """(what `send` raises, None when it succeeds; whether something paid for
    may still mine, which keeps a split's reservation)."""
    raw = type(_FAULTS[fault]())
    phase = _phase(scenario, point)
    if phase == "resize":
        # The refused transaction can never mine, the retry is not signed yet.
        return (raw if fault == "interrupt" else GasTopUpTimeoutError), False
    if phase == "top-up":
        # No answer leaves the top-up free to mine. The retry's top-up has a
        # refused signature in front of it, so its failure is an answer for
        # the caller (503).
        unseen = fault in ("timeout", "never-connected", "read-timeout", "interrupt")
        if fault in ("timeout", "dropped"):
            return GasTopUpTimeoutError, unseen
        if fault == "interrupt" or scenario == "straight":
            return raw, unseen
        return GasTopUpTimeoutError, unseen
    # A send: the first try of a call, or the refused call's one retry.
    if fault in ("fee-low", "balance-low"):
        if phase == "send":
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
def test_every_failure_point_frees_the_lock_sends_nothing_more_and_books_right(
    scenario, kind, point, fault
):
    db = fresh_test_db()
    user = _user(db)
    calls = [_Call(f"call{i}") for i in range(3 if kind == "onboarding" else 1)]
    retry = {
        "prices": (1_000, 1_500),
        "balances": (0, len(calls) * NEED),
        "refusals": [_refused(SKALED_FEE_LOW)],
    }
    chain = _FaultyChain(
        _point(point), _FAULTS[fault](), **(retry if scenario == "retry" else {})
    )
    sponsor = UserGasSponsor(db, chain, _settings())  # type: ignore[arg-type]
    raised: BaseException | None = None
    try:
        with sponsor.locked(user):
            sponsor.send(user, calls, kind)  # type: ignore[arg-type]
    except BaseException as exc:  # KeyboardInterrupt included
        raised = exc

    assert chain.fired, f"{point} is not where the calls are"
    with sponsor.locked(user):  # the lock is free
        pass
    expected, unseen = _expected(scenario, point, fault)
    assert (None if raised is None else type(raised)) is expected, repr(raised)
    if raised is not None:
        assert chain.after == []  # nothing, and no send, after the failure
        if isinstance(raised, Exception):
            assert _status_of(raised) == _STATUS.get(type(raised), 500)
    # Each mined top-up's transfer and each receipt, plus what is left of the
    # reservation when something may still mine unseen.
    paid = len(chain.funded) * TRANSFER_GAS + chain.mined * 80_000
    reserved = 120_000 + TRANSFER_GAS if kind == "split" and unseen else 0
    assert _used(db, user) == max(paid, reserved)
