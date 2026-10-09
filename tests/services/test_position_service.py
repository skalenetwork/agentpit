"""`PositionService` decides what reaches `UserGasSponsor` (the chain and the sponsor are fakes).

The sponsor tops a wallet up before every split, merge and claim, so the admin pays for whatever
this service lets through: a claim with nothing worth claiming, or on a market the chain has not
resolved, never reaches `send`; every chain read runs inside the user's lock; a row is written
only after the sponsor reports success; a claim is logged at the payout its receipt reports.

Every transaction the sponsor signs gets an intent row in `pending_user_txs` before it is
broadcast. Once its receipt is in, the row becomes the SPLIT / MERGE / REDEEM row; a refusal or a
revert removes it; when nobody knows how it ended (no receipt in time, no answer to the
broadcast) it stays, the caller gets `TransactionPendingError` (503), and the auto-redeem pass
settles it later. tests/onchain/test_sponsored_positions.py and test_pending_user_txs.py prove
the same against anvil.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field

import psycopg_pool
import pytest
import requests
from urllib3.exceptions import MaxRetryError, NewConnectionError
from web3.exceptions import BadResponseFormat, TimeExhausted, Web3RPCError

from agentpit.api.deps import get_position_service
from agentpit.config import Settings
from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.market_state import MarketState
from agentpit.datastructures.split_position_request import (
    MergePositionRequest,
    SplitPositionRequest,
)
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import (
    AdminGasPausedError,
    GasPriceMovedError,
    GasTopUpTimeoutError,
    InsufficientBalanceError,
    InsufficientGasError,
    MarketStateError,
    NothingToClaimError,
    TransactionInProgressError,
    TransactionPendingError,
    TransactionRevertedError,
)
from agentpit.onchain.tx_sender import TxDropped
from agentpit.services.gas_sponsor import UserGasSponsor
from agentpit.services.pending_user_txs import _PENDING_TTL_SECONDS
from agentpit.services.position_service import PositionService
from tests.db_helpers import fresh_test_db

_LOGGER = "agentpit.services.position_service"
_CONDITION = "0x" + "ab" * 32
_CID = bytes.fromhex(_CONDITION[2:])
_YES, _NO = "7001", "7002"
_ACTIONS = ["split", "merge", "redeem"]
# What each action's intent row carries: the final row's type and details, with no amount yet
# for a claim (the receipt has not said what it paid).
_INTENT = {
    "split": ("SPLIT", {"amount": 40_000_000}),
    "merge": ("MERGE", {"amount": 40_000_000}),
    "redeem": ("REDEEM", {}),
}


class _FakeChain:
    """The reads `PositionService` gates on, and call builders that return plain tuples, so a
    test sees exactly which call went to the sponsor. `reads` records every read, in order. An
    exception among the `usd` answers is raised by that read. `redeemed_payout` reads the figure
    off the receipt, as the real one decodes it, and remembers whose payout it was asked for."""

    def __init__(self, *, vector=(1, [1, 0]), balances=(0, 0), usd=(0,)):
        self.vector = vector
        self.balances = dict(zip((int(_YES), int(_NO)), balances))
        self._usd = list(usd)  # successive usd_balance answers; the last repeats
        self.reads: list[str] = []
        self.payout_reads: list[tuple[dict, str]] = []

    def payout_vector(self, condition_id, outcome_count=2):
        self.reads.append("payout_vector")
        return self.vector

    def ctf_balances(self, address, token_ids):
        self.reads.append("ctf_balances")
        return [self.balances[t] for t in token_ids]

    def ctf_balance(self, address, token_id):
        self.reads.append("ctf_balance")
        return self.balances[token_id]

    def usd_balance(self, address):
        self.reads.append("usd_balance")
        answer = self._usd.pop(0) if len(self._usd) > 1 else self._usd[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def redeemed_payout(self, receipt, redeemer):
        self.reads.append("redeemed_payout")
        self.payout_reads.append((receipt, redeemer))
        return receipt["payout"]

    def redeem_call(self, condition_id, partition):
        return ("redeemPositions", condition_id, partition)

    def split_call(self, condition_id, partition, amount):
        return ("splitPosition", condition_id, partition, amount)

    def merge_call(self, condition_id, partition, amount):
        return ("mergePositions", condition_id, partition, amount)


@dataclass
class _FakeSponsor:
    """Records each `send` as (calls, kind, whether the lock was held). `busy` makes `locked`
    refuse as a held lock does; `fail` is raised from `send` after it is recorded, as a reverted
    transaction is, or before anything is signed with `fail_unsigned` (a failed read or top-up).
    Each receipt carries `payout`, what the fake chain reads back. `log` gets "send" appended by
    every `send`, so a test can tell which chain reads came before it and which after.

    Each call is signed `signs` times before `fail` (2: refused at import and signed again at
    the new size, as the real sponsor's one retry does); every hash goes to `on_signed` and
    `hashes`. `during_send` runs once the calls are signed. `during_top_up` runs where the real
    top-up would mine, the window in which the world can change; `before_send` follows it, as
    the real sponsor's does, and anything it raises stops the send before anything is signed."""

    busy: bool = False
    fail: Exception | None = None
    min_claim_micro: int = 10_000
    payout: int = 0
    log: list | None = None
    signs: int = 1
    fail_unsigned: bool = False
    during_send: Callable[[], None] | None = None
    during_top_up: Callable[[], None] | None = None
    held: bool = field(default=False, init=False)
    sent: list[tuple[list, str, bool]] = field(default_factory=list, init=False)
    hashes: list[str] = field(default_factory=list, init=False)

    @contextmanager
    def locked(self, user):
        if self.busy:
            raise TransactionInProgressError()
        self.held = True
        try:
            yield
        finally:
            self.held = False

    def send(self, user, calls, kind, *, on_signed=None, before_send=None):
        self.sent.append((calls, kind, self.held))
        if self.log is not None:
            self.log.append("send")
        if self.fail is not None and self.fail_unsigned:
            raise self.fail
        if self.during_top_up is not None:
            self.during_top_up()
        if before_send is not None:
            before_send()
        for i in range(len(calls)):
            for _ in range(self.signs):
                self.hashes.append("0x%064x" % (len(self.hashes) + 1))
                if on_signed is not None:
                    on_signed(i, self.hashes[-1])
        if self.during_send is not None:
            self.during_send()
        if self.fail is not None:
            raise self.fail
        return [{"status": 1, "payout": self.payout} for _ in calls]


def _setup(state: MarketState):
    """A user and one binary market (YES=7001, NO=7002), ACTIVE or resolved to YES."""
    db = fresh_test_db()
    with db.write() as conn:
        user_id, _acct, _key = TableWrite.create_user(
            conn, email="holder@x.com", password_hash="x", handle=None
        )
        market = TableWrite.create_market(
            conn,
            CreateMarketRequest(
                question="Gate?",
                description="d",
                erc1155_tokens=[(_YES, "Yes"), (_NO, "No")],
                slug="gate",
                condition_id=ConditionId(_CONDITION),
                state=MarketState.ACTIVE,
            ),
            is_polygon_market=False,
        )
        if state == MarketState.RESOLVED:
            TableWrite.resolve_market(conn, market_id=market.market_id, winning_outcome_index=0)
        user = TableRead.get_user_by_userid(conn, user_id)
    assert user is not None
    return db, user, market.market_id


def _ready(action):
    """A user, a market in the state `action` needs, and a chain on which `action` passes its
    checks: 100 of each token and 100 apUSD."""
    db, user, mid = _setup(MarketState.RESOLVED if action == "redeem" else MarketState.ACTIVE)
    chain = _FakeChain(balances=(100_000_000, 100_000_000), usd=(100_000_000,))
    return db, user, mid, chain


def _service(db, chain, sponsor) -> PositionService:
    return PositionService(db, chain, sponsor)  # type: ignore[arg-type]


def _rows(db, user) -> list[str]:
    query = "SELECT TRANSACTION_TYPE FROM transactions WHERE API_KEY = %s"
    with db.read() as conn:
        return [r["TRANSACTION_TYPE"] for r in conn.execute(query, (user.api_key,)).fetchall()]


def _redeem_amounts(db, user) -> list[int]:
    """`collateral_amount` of each REDEEM row, as the profile page reads it."""
    query = "SELECT DETAILS FROM transactions WHERE API_KEY = %s AND TRANSACTION_TYPE = 'REDEEM'"
    with db.read() as conn:
        rows = conn.execute(query, (user.api_key,)).fetchall()
    return [json.loads(r["DETAILS"])["collateral_amount"] for r in rows]


def _pending(db) -> list[tuple[str, str, str, int | None, dict]]:
    """Every intent row: (hash, api key, type, market, details)."""
    with db.read() as conn:
        rows = TableRead.list_pending_user_txs(conn)
    return [(r.tx_hash, r.api_key, r.transaction_type, r.market_id, r.details) for r in rows]


def _write_pending(db, user, market_id, *, age: int, tx_hash="0x" + "cd" * 32):
    with db.write() as conn:
        TableWrite.insert_pending_user_tx(
            conn, tx_hash, user.api_key, "REDEEM", market_id, {}, created_at=int(time.time()) - age
        )


def _act(service, action, user, market_id, amount=40_000_000):
    if action == "split":
        return service.split(user, market_id, SplitPositionRequest(amount=amount))
    if action == "merge":
        return service.merge(user, market_id, MergePositionRequest(amount=amount))
    return service.redeem(user, market_id)


# --- claim -------------------------------------------------------------------


def test_a_winning_claim_is_sent_under_the_lock_and_logged(caplog):
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    db, user, mid = _setup(MarketState.RESOLVED)
    chain = _FakeChain(balances=(100_000_000, 100_000_000), usd=(100_000_005,))
    sponsor = _FakeSponsor(payout=100_000_000)

    out = _service(db, chain, sponsor).redeem(user, mid)

    # One call over the whole partition: the losing tokens burn in it too.
    assert sponsor.sent == [([("redeemPositions", _CID, [1, 2])], "claim", True)]
    assert out.collateral_amount == 100_000_000
    assert out.new_usdc_balance == 100_000_005
    assert _rows(db, user) == ["REDEEM"]
    assert _redeem_amounts(db, user) == [100_000_000]
    # The payout is read from the claim's receipt, for the claimant, and a payout there is no
    # surprise to log.
    assert chain.payout_reads == [({"status": 1, "payout": 100_000_000}, user.eth_address)]
    assert [r for r in caplog.records if r.name == _LOGGER] == []


# The wallet's apUSD moves while a claim is in flight (a fill, a mint, a transfer out), so a
# difference of two balance reads would credit or debit the claim; the receipt's
# `PayoutRedemption` is the figure, for the response and the REDEEM row alike.
@pytest.mark.parametrize(
    "usd",
    [(5, 70_000_005), (5, 107_000_005), (5, 5)],
    ids=["a-debit-of-30-while-claiming", "a-credit-of-7-while-claiming", "a-debit-as-big-as-payout"],
)
def test_the_claim_is_the_payout_in_the_receipt_whatever_the_balance_did(usd):
    db, user, mid = _setup(MarketState.RESOLVED)
    chain = _FakeChain(balances=(100_000_000, 0), usd=usd)
    sponsor = _FakeSponsor(payout=100_000_000)

    out = _service(db, chain, sponsor).redeem(user, mid)

    assert out.collateral_amount == 100_000_000
    assert _redeem_amounts(db, user) == [100_000_000]


def test_a_claim_that_mined_with_no_payout_is_no_claim_and_leaves_no_row(caplog):
    # The gate computed a payout, yet the receipt names none paid to the claimant (the tokens
    # left in between, a payout-vector mismatch...). A REDEEM row at zero would read as a lost
    # market and auto-redeem would count a claim made: so no row, the intent row goes, and the
    # caller hears `NothingToClaimError` (400). The surprise is logged with the market and the
    # transaction, never the key.
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    db, user, mid = _setup(MarketState.RESOLVED)
    chain = _FakeChain(balances=(100_000_000, 0), usd=(5,))
    sponsor = _FakeSponsor(payout=0)

    with pytest.raises(NothingToClaimError, match="nothing to claim"):
        _service(db, chain, sponsor).redeem(user, mid)

    assert len(sponsor.hashes) == 1  # it did go out, and mined
    assert _rows(db, user) == []
    assert _pending(db) == []
    warnings = [r for r in caplog.records if r.name == _LOGGER and r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert f"market {mid}" in message
    assert sponsor.hashes[0] in message
    assert user.api_key not in message


def test_the_new_balance_is_one_fresh_read_after_the_claim_and_none_before():
    db, user, mid = _setup(MarketState.RESOLVED)
    chain = _FakeChain(balances=(100_000_000, 0), usd=(250_000_000,))
    sponsor = _FakeSponsor(payout=100_000_000, log=chain.reads)

    out = _service(db, chain, sponsor).redeem(user, mid)

    sent_at = chain.reads.index("send")
    assert "usd_balance" not in chain.reads[:sent_at]
    assert chain.reads[sent_at:].count("usd_balance") == 1
    assert out.new_usdc_balance == 250_000_000


# The setting is validated to be at least 1, but the gate does not lean on it: a claim that
# pays nothing is pure admin gas, so it is refused even if the minimum were 0.
@pytest.mark.parametrize(
    ("balances", "minimum"),
    [
        pytest.param((0, 0), 10_000, id="zero-holdings"),
        pytest.param((0, 50_000_000), 10_000, id="losing-tokens-only"),
        pytest.param((9_999, 0), 10_000, id="dust-below-a-cent"),
        pytest.param((0, 0), 0, id="zero-holdings-minimum-0"),
        pytest.param((0, 50_000_000), 0, id="losing-tokens-only-minimum-0"),
        pytest.param((0, 0), 1, id="zero-holdings-minimum-1"),
        pytest.param((0, 50_000_000), 1, id="losing-tokens-only-minimum-1"),
    ],
)
def test_nothing_worth_claiming_never_reaches_the_sponsor(balances, minimum):
    db, user, mid = _setup(MarketState.RESOLVED)
    sponsor = _FakeSponsor(min_claim_micro=minimum)
    with pytest.raises(NothingToClaimError, match="nothing to claim"):
        _service(db, _FakeChain(balances=balances), sponsor).redeem(user, mid)
    assert sponsor.sent == []
    assert _rows(db, user) == []


# payout = sum(balance_i * numerator_i // denominator), against the minimum (10_000: $0.01).
@pytest.mark.parametrize(
    ("vector", "balances", "claimed"),
    [
        pytest.param((1, [1, 0]), (10_000, 0), True, id="exactly-the-minimum"),
        pytest.param((2, [1, 1]), (15_000, 5_000), True, id="even-split-reaching-it"),
        pytest.param((2, [1, 1]), (15_000, 4_999), False, id="even-split-a-micro-short"),
    ],
)
def test_the_payout_weighs_each_balance_by_its_numerator(vector, balances, claimed):
    db, user, mid = _setup(MarketState.RESOLVED)
    sponsor = _FakeSponsor(payout=1)
    service = _service(db, _FakeChain(vector=vector, balances=balances), sponsor)
    if claimed:
        service.redeem(user, mid)
        assert [kind for _calls, kind, _held in sponsor.sent] == ["claim"]
    else:
        with pytest.raises(NothingToClaimError):
            service.redeem(user, mid)
        assert sponsor.sent == []


def test_the_minimum_comes_from_the_sponsor():
    db, user, mid = _setup(MarketState.RESOLVED)
    sponsor = _FakeSponsor(min_claim_micro=1, payout=1)
    _service(db, _FakeChain(balances=(1, 0)), sponsor).redeem(user, mid)
    assert len(sponsor.sent) == 1


def test_a_market_the_chain_has_not_resolved_is_refused_without_a_send():
    # RESOLVED in the database, no `reportPayouts` on chain: `redeemPositions` would revert,
    # after the admin had paid for the top-up in front of it.
    db, user, mid = _setup(MarketState.RESOLVED)
    chain = _FakeChain(vector=(0, [0, 0]), balances=(100_000_000, 0))
    sponsor = _FakeSponsor()
    with pytest.raises(MarketStateError, match="not resolved on chain"):
        _service(db, chain, sponsor).redeem(user, mid)
    assert sponsor.sent == []
    assert _rows(db, user) == []


def test_a_vector_the_caller_already_read_is_not_read_again():
    db, user, mid = _setup(MarketState.RESOLVED)
    # The chain's own vector would refuse; the pre-read one is what counts.
    chain = _FakeChain(vector=(0, [0, 0]), balances=(100_000_000, 0))
    sponsor = _FakeSponsor(payout=100_000_000)
    _service(db, chain, sponsor).redeem(user, mid, payout_vector=(1, [1, 0]))
    assert "payout_vector" not in chain.reads
    assert len(sponsor.sent) == 1


def test_a_market_the_database_has_not_resolved_is_refused_before_the_lock():
    # Checked before the lock, so a busy account still hears the real reason, and nothing
    # touches the chain.
    db, user, mid = _setup(MarketState.ACTIVE)
    chain = _FakeChain()
    with pytest.raises(MarketStateError, match="not resolved yet"):
        _service(db, chain, _FakeSponsor(busy=True)).redeem(user, mid)
    assert chain.reads == []


# --- every action --------------------------------------------------------------


# The pre-checks and the claim gate live inside the lock: a second request for the same
# account is a 409, not a race against the first.
@pytest.mark.parametrize("action", _ACTIONS)
def test_a_held_lock_refuses_before_any_chain_read(action):
    db, user, mid, chain = _ready(action)
    with pytest.raises(TransactionInProgressError):
        _act(_service(db, chain, _FakeSponsor(busy=True)), action, user, mid)
    assert chain.reads == []
    assert _rows(db, user) == []


@pytest.mark.parametrize("action", _ACTIONS)
def test_a_reverted_transaction_writes_no_row(action):
    db, user, mid, chain = _ready(action)
    sponsor = _FakeSponsor(fail=TransactionRevertedError("transaction reverted"))
    with pytest.raises(TransactionRevertedError):
        _act(_service(db, chain, sponsor), action, user, mid)
    assert len(sponsor.sent) == 1
    assert _rows(db, user) == []
    assert _pending(db) == []  # it mined and failed: nothing is unknown


# --- the intent row ------------------------------------------------------------

@pytest.mark.parametrize("action", _ACTIONS)
def test_the_intent_row_is_written_before_the_send_and_becomes_the_row(action):
    db, user, mid, chain = _ready(action)
    seen: list[list] = []
    sponsor = _FakeSponsor(payout=100_000_000, during_send=lambda: seen.append(_pending(db)))

    _act(_service(db, chain, sponsor), action, user, mid)

    kind, details = _INTENT[action]
    assert seen == [[(sponsor.hashes[0], user.api_key, kind, mid, details)]]
    assert _pending(db) == []
    assert _rows(db, user) == [kind]


@pytest.mark.parametrize("action", _ACTIONS)
@pytest.mark.parametrize(
    "error",
    [
        pytest.param(TimeExhausted("no receipt in 30s"), id="receipt-timeout"),
        pytest.param(requests.ReadTimeout("read timed out"), id="read-timeout"),
        pytest.param(requests.ConnectionError("connection reset"), id="reset"),
    ],
)
def test_a_sent_transaction_nobody_heard_back_about_stays_pending(action, error, caplog):
    """It may mine yet. The intent row stays for the auto-redeem pass to
    settle, nothing is written to the history now, and the caller hears 503
    with "do not repeat it", not a 500."""
    caplog.set_level(logging.WARNING, logger="agentpit.services.position_service")
    db, user, mid, chain = _ready(action)
    sponsor = _FakeSponsor(fail=error)

    with pytest.raises(TransactionPendingError, match="do not repeat it") as caught:
        _act(_service(db, chain, sponsor), action, user, mid)

    assert caught.value.__cause__ is error
    kind, details = _INTENT[action]
    assert _pending(db) == [(sponsor.hashes[0], user.api_key, kind, mid, details)]
    assert _rows(db, user) == []
    warnings = [r for r in caplog.records if r.name == _LOGGER and r.levelno == logging.WARNING]
    assert len(warnings) == 1 and sponsor.hashes[0] in warnings[0].getMessage()


def _http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"{status} Error", response=response)


@pytest.mark.parametrize("action", _ACTIONS)
@pytest.mark.parametrize(
    "error",
    [
        # What the receipt poll can raise once `send_raw_transaction` has
        # returned: the node holds the transaction, so none of these says it
        # will not mine, whatever `classify_send_error` makes of the text.
        pytest.param(Web3RPCError("rate limit exceeded"), id="receipt-rpc-error"),
        pytest.param(_http_error(429), id="receipt-429-after-the-retries"),
        pytest.param(
            BadResponseFormat("no result in the response"), id="malformed-body"
        ),
        pytest.param(Web3RPCError("transaction already known"), id="node-holds-it"),
    ],
)
def test_an_error_after_the_node_took_the_transaction_stays_pending(
    action, error, caplog
):
    """Not every error that is neither a receipt timeout nor a transport error
    is an answer about the transaction. The receipt poll runs after the
    broadcast was accepted, and a JSON-RPC error on it, a 429 that outlives
    the provider's retries or a body that does not parse leave the transaction
    in the node, free to mine. Deleting its row would lose the history row and
    lift the duplicate guard at once, so a client retry splits twice. The row
    stays, and the caller hears 503, "do not repeat it"."""
    caplog.set_level(logging.WARNING, logger="agentpit.services.position_service")
    db, user, mid, chain = _ready(action)
    sponsor = _FakeSponsor(fail=error)  # one signature: nothing was refused

    with pytest.raises(TransactionPendingError, match="do not repeat it") as caught:
        _act(_service(db, chain, sponsor), action, user, mid)

    assert caught.value.__cause__ is error
    kind, details = _INTENT[action]
    assert _pending(db) == [(sponsor.hashes[0], user.api_key, kind, mid, details)]
    assert _rows(db, user) == []
    warnings = [
        r for r in caplog.records if r.name == _LOGGER and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1 and sponsor.hashes[0] in warnings[0].getMessage()


@pytest.mark.parametrize("action", _ACTIONS)
def test_no_answer_before_anything_was_signed_is_no_pending_transaction(action):
    """A read or a top-up that got no answer: no transaction of the user's
    exists, so there is nothing to wait for and the error is what it is."""
    db, user, mid, chain = _ready(action)
    error = requests.ConnectionError("connection reset")
    sponsor = _FakeSponsor(fail=error, fail_unsigned=True)

    with pytest.raises(requests.ConnectionError):
        _act(_service(db, chain, sponsor), action, user, mid)

    assert _pending(db) == []
    assert _rows(db, user) == []


@pytest.mark.parametrize("action", _ACTIONS)
@pytest.mark.parametrize(
    "error",
    [
        pytest.param(InsufficientGasError("could not pay"), id="balance-low-twice-402"),
        pytest.param(
            Web3RPCError("Transaction gas price lower than current eth_gasPrice"),
            id="fee-low-twice",
        ),
        pytest.param(GasPriceMovedError(), id="fee-low-twice-503"),
        pytest.param(GasTopUpTimeoutError(), id="the-retrys-top-up-timed-out"),
        pytest.param(AdminGasPausedError(), id="the-retrys-top-up-is-paused"),
        pytest.param(
            TxDropped("its nonce went elsewhere"), id="the-retrys-top-up-dropped"
        ),
        pytest.param(Web3RPCError("nonce too low"), id="nonce-refusal"),
    ],
)
def test_a_transaction_the_node_refused_leaves_no_pending_row(action, error):
    """Signed, refused at import, signed again and refused (or never sent,
    because the retry's top-up failed): the node holds none of them, so
    neither leaves a row behind."""
    db, user, mid, chain = _ready(action)
    sponsor = _FakeSponsor(fail=error, signs=2)

    with pytest.raises(type(error)):
        _act(_service(db, chain, sponsor), action, user, mid)

    assert len(sponsor.hashes) == 2
    assert _pending(db) == []
    assert _rows(db, user) == []


@pytest.mark.parametrize("action", _ACTIONS)
@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            Web3RPCError("replacement transaction underpriced"), id="nonce-taken"
        ),
        pytest.param(Web3RPCError("nonce too low"), id="nonce-invalid"),
        pytest.param(Web3RPCError("transaction queue is full"), id="queue-full"),
        pytest.param(Web3RPCError("account balance is too low"), id="balance-low"),
        pytest.param(
            Web3RPCError("Transaction gas price lower than current eth_gasPrice"),
            id="fee-low",
        ),
    ],
)
def test_a_refusal_at_import_leaves_no_pending_row(action, error):
    """The answers that say the node did not take the transaction, on the
    first signature already: its row goes."""
    db, user, mid, chain = _ready(action)
    sponsor = _FakeSponsor(fail=error)

    with pytest.raises(Web3RPCError):
        _act(_service(db, chain, sponsor), action, user, mid)

    assert len(sponsor.hashes) == 1
    assert _pending(db) == []
    assert _rows(db, user) == []


@pytest.mark.parametrize("action", _ACTIONS)
def test_a_broadcast_that_never_reached_the_node_leaves_no_pending_row(action):
    """The connect itself was refused (`failed_before_connecting`): the node
    never saw the transaction, so it cannot mine and nothing is pending. The
    row goes and the error is what it is, instead of a 503 "do not repeat it"
    and 409s on the market until the auto-redeem pass drops the row."""
    db, user, mid, chain = _ready(action)
    error = requests.ConnectionError(
        MaxRetryError(
            None, "/", NewConnectionError(None, "Failed to establish a new connection")
        )
    )
    sponsor = _FakeSponsor(fail=error)

    with pytest.raises(requests.ConnectionError):
        _act(_service(db, chain, sponsor), action, user, mid)

    assert len(sponsor.hashes) == 1
    assert _pending(db) == []
    assert _rows(db, user) == []


@pytest.mark.parametrize("action", _ACTIONS)
def test_a_resized_retry_replaces_the_refused_signatures_row(action):
    """The sponsor signs a call again only after the node refused it, so the
    first hash can never mine: its row goes as the second one's is written."""
    db, user, mid, chain = _ready(action)
    seen: list[list] = []
    sponsor = _FakeSponsor(
        payout=100_000_000, signs=2, during_send=lambda: seen.append(_pending(db))
    )

    _act(_service(db, chain, sponsor), action, user, mid)

    assert [[row[0] for row in rows] for rows in seen] == [[sponsor.hashes[1]]]
    assert _pending(db) == []
    assert _rows(db, user) == [_INTENT[action][0]]


@pytest.mark.parametrize("action", _ACTIONS)
def test_a_transaction_confirmed_meanwhile_is_not_logged_twice(action):
    """The auto-redeem pass can settle the intent row while the request is
    still waiting for the same receipt. Whoever confirms second finds the row
    gone and writes nothing."""
    db, user, mid, chain = _ready(action)

    def reconciler_first():
        with db.write() as conn:
            TableWrite.confirm_pending_user_tx(
                conn, sponsor.hashes[0], {"collateral_amount": 100_000_000}
            )

    sponsor = _FakeSponsor(payout=100_000_000, during_send=reconciler_first)

    _act(_service(db, chain, sponsor), action, user, mid)

    assert _rows(db, user) == [_INTENT[action][0]]
    assert _pending(db) == []


@pytest.mark.parametrize("action", _ACTIONS)
def test_a_mined_transaction_whose_row_cannot_be_written_stays_pending(
    action, monkeypatch, caplog
):
    """It mined, and the history row could not be written (the pool timed
    out). The intent row is still there, so the auto-redeem pass writes the
    row later; the caller hears what it would for an unknown outcome, and the
    log says whose transaction it was."""
    db, user, mid, chain = _ready(action)
    sponsor = _FakeSponsor(payout=100_000_000)

    def broken(*_a, **_k):
        raise psycopg_pool.PoolTimeout("couldn't get a connection after 30 sec")

    monkeypatch.setattr(TableWrite, "confirm_pending_user_tx", broken)

    with pytest.raises(TransactionPendingError):
        _act(_service(db, chain, sponsor), action, user, mid)

    assert [row[0] for row in _pending(db)] == sponsor.hashes
    assert _rows(db, user) == []
    errors = [r for r in caplog.records if r.name == _LOGGER and r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert user.user_id in errors[0].getMessage()
    assert str(mid) in errors[0].getMessage()


def test_a_claims_row_is_written_before_the_balance_is_read_again():
    """The claim mined: its REDEEM row does not hang on the read of the new
    balance that follows it."""
    db, user, mid = _setup(MarketState.RESOLVED)
    chain = _FakeChain(
        balances=(100_000_000, 0), usd=(requests.ReadTimeout("read timed out"),)
    )
    sponsor = _FakeSponsor(payout=100_000_000)

    with pytest.raises(requests.ReadTimeout):
        _service(db, chain, sponsor).redeem(user, mid)

    assert _redeem_amounts(db, user) == [100_000_000]
    assert _pending(db) == []


# --- the world changes between the gate and the send --------------------------


def _set_state(db, market_id: int, state: MarketState) -> None:
    with db.write() as conn:
        if state == MarketState.RESOLVED:  # a resolved market names its winner
            TableWrite.resolve_market(
                conn, market_id=market_id, winning_outcome_index=0
            )
        else:
            conn.execute(
                "UPDATE markets SET MARKET_STATE = %s WHERE MARKET_ID = %s",
                (state.value, market_id),
            )


@pytest.mark.parametrize(
    "state",
    [MarketState.RESOLVED, MarketState.CANCELLED],
)
def test_a_split_on_a_market_that_stopped_trading_during_the_top_up_is_refused(state):
    """`split` checks that the market is ACTIVE before it takes the lock, and the
    top-up that follows can take a block or several. A market resolved or
    cancelled in that window would take a split after all: a pair whose loser
    is worthless and whose winner is claimable. The sponsor's `before_send`
    reads the market again once the wallet is funded, and nothing is signed."""
    db, user, mid = _setup(MarketState.ACTIVE)
    chain = _FakeChain(usd=(100_000_000,))
    sponsor = _FakeSponsor(during_top_up=lambda: _set_state(db, mid, state))

    with pytest.raises(MarketStateError, match="split only runs on ACTIVE markets"):
        _service(db, chain, sponsor).split(
            user, mid, SplitPositionRequest(amount=40_000_000)
        )

    assert sponsor.hashes == []
    assert _pending(db) == []
    assert _rows(db, user) == []


@pytest.mark.parametrize(
    "balances",
    [
        pytest.param((0, 0), id="every-token-left"),
        pytest.param((0, 100_000_000), id="only-the-loser-is-left"),
        pytest.param((9_999, 0), id="dust-below-a-cent"),
    ],
)
def test_a_claim_whose_tokens_left_during_the_top_up_is_refused(balances):
    """The gate ran before the top-up. A resting SELL filled meanwhile, or a
    transfer out, can leave a position that would pay less than the minimum,
    and `redeemPositions` would then mine a payout of nothing at the admin's
    expense. The sponsor's `before_send` runs the gate again once the wallet
    is funded: the claim is refused as it would have been up front, and
    nothing is signed."""
    db, user, mid = _setup(MarketState.RESOLVED)
    chain = _FakeChain(balances=(100_000_000, 0), usd=(5,))

    def tokens_leave():
        chain.balances = dict(zip((int(_YES), int(_NO)), balances))

    sponsor = _FakeSponsor(payout=100_000_000, during_top_up=tokens_leave)

    with pytest.raises(NothingToClaimError, match="nothing to claim"):
        _service(db, chain, sponsor).redeem(user, mid)

    assert sponsor.hashes == []
    assert _pending(db) == []
    assert _rows(db, user) == []
    assert chain.reads.count("ctf_balances") == 2  # the gate, and again before the send


# --- an earlier transaction still pending -------------------------------------


def test_the_pending_ttl_is_ten_minutes():
    assert _PENDING_TTL_SECONDS == 600


@pytest.mark.parametrize("action", _ACTIONS)
def test_a_pending_transaction_on_the_market_refuses_another_before_any_read(action):
    """A retry of a split whose answer was lost would split twice. Until the
    first is settled (or ten minutes have passed), a split, merge or claim on
    the same market is a 409, refused inside the lock before any chain read."""
    db, user, mid, chain = _ready(action)
    _write_pending(db, user, mid, age=_PENDING_TTL_SECONDS - 5)
    sponsor = _FakeSponsor()

    with pytest.raises(TransactionInProgressError, match="not confirmed yet"):
        _act(_service(db, chain, sponsor), action, user, mid)

    assert chain.reads == []
    assert sponsor.sent == []


@pytest.mark.parametrize("action", _ACTIONS)
def test_an_expired_or_unrelated_pending_row_refuses_nothing(action):
    db, user, mid, chain = _ready(action)
    _write_pending(db, user, mid, age=_PENDING_TTL_SECONDS + 5, tx_hash="0x" + "01" * 32)
    _write_pending(db, user, mid + 1_000, age=0, tx_hash="0x" + "02" * 32)
    with db.write() as conn:
        other_id, _acct, _key = TableWrite.create_user(
            conn, email="other@x.com", password_hash="x", handle=None
        )
        other = TableRead.get_user_by_userid(conn, other_id)
    _write_pending(db, other, mid, age=0, tx_hash="0x" + "03" * 32)
    sponsor = _FakeSponsor(payout=100_000_000)

    _act(_service(db, chain, sponsor), action, user, mid)

    assert _rows(db, user) == [_INTENT[action][0]]


# --- split / merge -------------------------------------------------------------


def test_a_split_is_sent_under_the_lock_and_logged():
    db, user, mid = _setup(MarketState.ACTIVE)
    chain = _FakeChain(balances=(40_000_000, 40_000_000), usd=(100_000_000,))
    sponsor = _FakeSponsor()

    out = _service(db, chain, sponsor).split(
        user, mid, SplitPositionRequest(amount=40_000_000)
    )

    assert sponsor.sent == [
        ([("splitPosition", _CID, [1, 2], 40_000_000)], "split", True)
    ]
    assert out.collateral_amount == 40_000_000
    assert _rows(db, user) == ["SPLIT"]


def test_a_merge_is_sent_under_the_lock_and_logged():
    db, user, mid = _setup(MarketState.ACTIVE)
    chain = _FakeChain(balances=(40_000_000, 40_000_000))
    sponsor = _FakeSponsor()

    out = _service(db, chain, sponsor).merge(
        user, mid, MergePositionRequest(amount=15_000_000)
    )

    assert sponsor.sent == [
        ([("mergePositions", _CID, [1, 2], 15_000_000)], "merge", True)
    ]
    assert out.amount == 15_000_000
    assert _rows(db, user) == ["MERGE"]


@pytest.mark.parametrize(
    ("action", "balances", "usd"),
    [
        pytest.param("split", (0, 0), (39_999_999,), id="split-short-of-apUSD"),
        pytest.param("merge", (40_000_000, 39_999_999), (0,), id="merge-short-of-NO"),
    ],
)
def test_a_split_or_merge_without_the_funds_sends_nothing(action, balances, usd):
    db, user, mid = _setup(MarketState.ACTIVE)
    sponsor = _FakeSponsor()
    service = _service(db, _FakeChain(balances=balances, usd=usd), sponsor)
    with pytest.raises(InsufficientBalanceError, match="need 40000000"):
        _act(service, action, user, mid)
    assert sponsor.sent == []
    assert _rows(db, user) == []


# --- wiring --------------------------------------------------------------------


def test_the_dependency_sponsors_with_the_apps_settings():
    """The route's service carries a sponsor built from the app's settings,
    so AGENTPIT_MIN_CLAIM_MICRO and the kill switch reach every request."""
    service = get_position_service(
        fresh_test_db(), object(), Settings(min_claim_micro=42)  # type: ignore[arg-type]
    )
    sponsor = service._sponsor  # noqa: SLF001
    assert isinstance(sponsor, UserGasSponsor)
    assert sponsor.min_claim_micro == 42
