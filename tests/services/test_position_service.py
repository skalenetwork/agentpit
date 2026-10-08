"""`PositionService` decides what reaches `UserGasSponsor`.

The sponsor tops a wallet up before every split, merge and claim, so the admin
pays for whatever this service lets through. These tests pin the gate in
front of it with fakes for the chain and the sponsor: a claim with nothing
worth claiming, or on a market the chain has not resolved, never reaches
`send`; every chain read runs inside the user's lock; and a row is written only
after the sponsor reports success. tests/onchain/test_sponsored_positions.py
proves the same against anvil.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

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
    InsufficientBalanceError,
    MarketStateError,
    NothingToClaimError,
    TransactionInProgressError,
    TransactionRevertedError,
)
from agentpit.services.gas_sponsor import UserGasSponsor
from agentpit.services.position_service import PositionService
from tests.db_helpers import fresh_test_db

_CONDITION = "0x" + "ab" * 32
_CID = bytes.fromhex(_CONDITION[2:])
_YES, _NO = "7001", "7002"


class _FakeChain:
    """The reads `PositionService` gates on, and call builders that return
    plain tuples, so a test sees exactly which call went to the sponsor.
    `reads` records every chain read, in order."""

    def __init__(self, *, vector=(1, [1, 0]), balances=(0, 0), usd=(0,)):
        self.vector = vector
        self.balances = dict(zip((int(_YES), int(_NO)), balances))
        self._usd = list(usd)  # successive usd_balance answers; the last repeats
        self.reads: list[str] = []

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
        return self._usd.pop(0) if len(self._usd) > 1 else self._usd[0]

    def redeem_call(self, condition_id, partition):
        return ("redeemPositions", condition_id, partition)

    def split_call(self, condition_id, partition, amount):
        return ("splitPosition", condition_id, partition, amount)

    def merge_call(self, condition_id, partition, amount):
        return ("mergePositions", condition_id, partition, amount)


class _FakeSponsor:
    """Records each `send` as (calls, kind, whether the lock was held).
    `busy` makes `locked` refuse as a held lock does; `fail` is raised from
    `send` after it is recorded, as a reverted transaction is."""

    def __init__(self, *, busy=False, fail=None, min_claim_micro=10_000):
        self._busy = busy
        self._fail = fail
        self._min = min_claim_micro
        self.held = False
        self.sent: list[tuple[list, str, bool]] = []

    @property
    def min_claim_micro(self) -> int:
        return self._min

    @contextmanager
    def locked(self, user):
        if self._busy:
            raise TransactionInProgressError()
        self.held = True
        try:
            yield
        finally:
            self.held = False

    def send(self, user, calls, kind):
        self.sent.append((calls, kind, self.held))
        if self._fail is not None:
            raise self._fail
        return [{"status": 1} for _ in calls]


def _setup(state: MarketState):
    """A user and one binary market (YES=7001, NO=7002), left ACTIVE or
    resolved to YES in the database."""
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
            TableWrite.resolve_market(
                conn, market_id=market.market_id, winning_outcome_index=0
            )
        user = TableRead.get_user_by_userid(conn, user_id)
    assert user is not None
    return db, user, market.market_id


def _service(db, chain, sponsor) -> PositionService:
    return PositionService(db, chain, sponsor)  # type: ignore[arg-type]


def _rows(db, user) -> list[str]:
    with db.read() as conn:
        return [
            r["TRANSACTION_TYPE"]
            for r in conn.execute(
                "SELECT TRANSACTION_TYPE FROM transactions WHERE API_KEY = %s",
                (user.api_key,),
            ).fetchall()
        ]


def _act(service, action, user, market_id):
    if action == "split":
        return service.split(user, market_id, SplitPositionRequest(amount=40_000_000))
    if action == "merge":
        return service.merge(user, market_id, MergePositionRequest(amount=40_000_000))
    return service.redeem(user, market_id)


# --- claim -------------------------------------------------------------------


def test_a_winning_claim_is_sent_under_the_lock_and_logged():
    db, user, mid = _setup(MarketState.RESOLVED)
    chain = _FakeChain(balances=(100_000_000, 100_000_000), usd=(5, 100_000_005))
    sponsor = _FakeSponsor()

    out = _service(db, chain, sponsor).redeem(user, mid)

    # One call over the whole partition: the losing tokens burn in it too.
    assert sponsor.sent == [([("redeemPositions", _CID, [1, 2])], "claim", True)]
    assert out.collateral_amount == 100_000_000
    assert out.new_usdc_balance == 100_000_005
    assert _rows(db, user) == ["REDEEM"]


@pytest.mark.parametrize(
    "balances",
    [
        pytest.param((0, 0), id="zero-holdings"),
        pytest.param((0, 50_000_000), id="losing-tokens-only"),
        pytest.param((9_999, 0), id="dust-below-a-cent"),
    ],
)
def test_nothing_worth_claiming_never_reaches_the_sponsor(balances):
    db, user, mid = _setup(MarketState.RESOLVED)
    sponsor = _FakeSponsor()
    with pytest.raises(NothingToClaimError, match="nothing to claim"):
        _service(db, _FakeChain(balances=balances), sponsor).redeem(user, mid)
    assert sponsor.sent == []
    assert _rows(db, user) == []


@pytest.mark.parametrize(
    ("vector", "balances", "claimed"),
    [
        pytest.param((1, [1, 0]), (10_000, 0), True, id="exactly-the-minimum"),
        pytest.param((2, [1, 1]), (15_000, 5_000), True, id="even-split-reaching-it"),
        pytest.param((2, [1, 1]), (15_000, 4_999), False, id="even-split-a-micro-short"),
    ],
)
def test_the_payout_weighs_each_balance_by_its_numerator(vector, balances, claimed):
    """payout = sum(balance_i * numerator_i // denominator), against the
    minimum (10_000, $0.01) inclusive."""
    db, user, mid = _setup(MarketState.RESOLVED)
    sponsor = _FakeSponsor()
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
    sponsor = _FakeSponsor(min_claim_micro=1)
    _service(db, _FakeChain(balances=(1, 0)), sponsor).redeem(user, mid)
    assert len(sponsor.sent) == 1


def test_a_market_the_chain_has_not_resolved_is_refused_without_a_send():
    """RESOLVED in the database, no `reportPayouts` on chain: `redeemPositions`
    would revert, after the admin had paid for the top-up in front of it."""
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
    sponsor = _FakeSponsor()
    _service(db, chain, sponsor).redeem(user, mid, payout_vector=(1, [1, 0]))
    assert "payout_vector" not in chain.reads
    assert len(sponsor.sent) == 1


def test_a_market_the_database_has_not_resolved_is_refused_before_the_lock():
    """Checked before the lock, so a busy account still hears the real
    reason, and nothing touches the chain."""
    db, user, mid = _setup(MarketState.ACTIVE)
    chain = _FakeChain()
    with pytest.raises(MarketStateError, match="not resolved yet"):
        _service(db, chain, _FakeSponsor(busy=True)).redeem(user, mid)
    assert chain.reads == []


# --- every action --------------------------------------------------------------


@pytest.mark.parametrize("action", ["split", "merge", "redeem"])
def test_a_held_lock_refuses_before_any_chain_read(action):
    """The pre-checks and the claim gate live inside the lock: a second
    request for the same account is a 409, not a race against the first."""
    db, user, mid = _setup(
        MarketState.RESOLVED if action == "redeem" else MarketState.ACTIVE
    )
    chain = _FakeChain(balances=(100_000_000, 100_000_000), usd=(100_000_000,))
    with pytest.raises(TransactionInProgressError):
        _act(_service(db, chain, _FakeSponsor(busy=True)), action, user, mid)
    assert chain.reads == []
    assert _rows(db, user) == []


@pytest.mark.parametrize("action", ["split", "merge", "redeem"])
def test_a_reverted_transaction_writes_no_row(action):
    db, user, mid = _setup(
        MarketState.RESOLVED if action == "redeem" else MarketState.ACTIVE
    )
    chain = _FakeChain(balances=(100_000_000, 100_000_000), usd=(100_000_000,))
    sponsor = _FakeSponsor(fail=TransactionRevertedError("transaction reverted"))
    with pytest.raises(TransactionRevertedError):
        _act(_service(db, chain, sponsor), action, user, mid)
    assert len(sponsor.sent) == 1
    assert _rows(db, user) == []


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
