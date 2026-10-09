"""What one auto-redeem pass decides, with the chain faked.

The admin pays for every claim (gasless claims), so who a pass claims for, how many
and what it does when one cannot go out are questions of cost. `redeem` is a stub:
tests/onchain/test_auto_redeem.py covers the transaction.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from collections import namedtuple

import pytest

from agentpit.config import Settings
from agentpit.datastructures.user import User
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain import exceptions as errors
from agentpit.services import gas_sponsor, market_service
from agentpit.services.market_service import redeem_resolved_markets
from agentpit.services.position_service import PositionService
from tests.db_helpers import fresh_test_db

_LOGGER = "agentpit.services.market_service"
_ONE_USD = 1_000_000  # micro-apUSD, well above the $0.01 minimum
_H1 = "0x" + "e1" * 32  # the one pending transaction hash the tests use
# Taken before the fixtures swap them out, for the test that wants the real ones.
_REAL_SPONSOR = market_service.UserGasSponsor
_REAL_REDEEM = PositionService.redeem
_BACKOFF = market_service._claim_backoff_until  # noqa: SLF001
_REFUSED = market_service._REFUSED_BACKOFF_SECONDS  # noqa: SLF001
_REVERT = market_service._REVERT_BACKOFF_SECONDS  # noqa: SLF001

_Market = namedtuple("_Market", "id yes no")
_Claim = namedtuple("_Claim", "user_id market_id vector")


def _raise_next(queue: list[Exception | None]) -> None:
    """Raise the next queued RPC error; a None lets that read answer."""
    if queue and (error := queue.pop(0)) is not None:
        raise error


class _Chain:
    """The two reads a pass makes, and nothing else: anything more it touched
    (fund_gas, send_as_user, ...) would raise here. Unset balances read 0."""

    def __init__(self) -> None:
        self.vector = (1, [1, 0])
        self.held: dict[str, dict[int, int]] = {}
        self.vector_reads = self.balance_reads = 0
        self.vector_errors: list[Exception | None] = []
        self.balance_errors: list[Exception | None] = []

    def hold(self, address: str, token: str, amount: int) -> None:
        self.held.setdefault(address.lower(), {})[int(token)] = amount

    def payout_vector(self, condition_id: bytes, outcome_count: int = 2):
        self.vector_reads += 1
        _raise_next(self.vector_errors)
        return self.vector

    def ctf_balances(self, address: str, token_ids: list[int]) -> list[int]:
        self.balance_reads += 1
        _raise_next(self.balance_errors)
        held = self.held.get(address.lower(), {})
        return [held.get(t, 0) for t in token_ids]


class _ReceiptChain(_Chain):
    """`_Chain` plus what the pass's reconciler asks: `receipts[tx_hash]`."""

    def __init__(self) -> None:
        super().__init__()
        self.receipts: dict[str, dict | None] = {}

    def transaction_receipt(self, tx_hash: str) -> dict | None:
        return self.receipts.get(tx_hash)

    def redeemed_payout(self, receipt: dict, redeemer: str) -> int:
        return receipt["payout"]


def _settings(*, cap: int = 20) -> Settings:
    return Settings(
        _env_file=None, min_claim_micro=10_000, auto_redeem_max_per_pass=cap
    )


class _World:
    """A fresh database, a fake chain and a stubbed `redeem` that records every
    claim in `calls` and raises `outcomes[(user_id, market_id)]` when one is set."""

    def __init__(self, chain: _Chain) -> None:
        self.db, self.chain = fresh_test_db(), chain
        self.calls: list[_Claim] = []
        self.outcomes: dict[tuple[str, int], Exception] = {}

    def redeem(self, user: User, market_id: int, *, payout_vector=None) -> None:
        self.calls.append(_Claim(user.user_id, market_id, payout_vector))
        if (exc := self.outcomes.get((user.user_id, market_id))) is not None:
            raise exc

    @property
    def claimed_markets(self) -> list[int]:
        return [c.market_id for c in self.calls]

    @property
    def claimed_users(self) -> list[str]:
        return [c.user_id for c in self.calls]

    def market(self, tokens: tuple[str, str] | None = None) -> _Market:
        """A RESOLVED market whose YES (index 0) won; random token ids unless given."""
        yes, no = tokens or [str(secrets.randbits(63)) for _ in range(2)]
        tag = secrets.token_hex(4)
        with self.db.write() as conn:
            row = conn.execute(
                "INSERT INTO markets (CONDITION_ID, QUESTION_ID, QUESTION, SLUG, DESCRIPTION, "
                "ERC1155_TOKENS, START_DATE, MARKET_STATE, PAYOUTS) "
                "VALUES (%s, %s, %s, %s, 'd', %s, 100, 'RESOLVED', '{1,0}') RETURNING MARKET_ID",
                (
                    f"0x{secrets.token_hex(32)}",
                    f"0x{secrets.token_hex(32)}",
                    f"Won {tag}?",
                    f"won-{tag}",
                    json.dumps([[yes, "YES"], [no, "NO"]]),
                ),
            ).fetchone()
        return _Market(row["MARKET_ID"], yes, no)

    def account(self, *, opted_in: bool = True) -> User:
        """An account with no trade (found only via a SPLIT row or a pending one)."""
        email = f"acct-{secrets.token_hex(4)}@example.com"
        with self.db.write() as conn:
            user_id, _acct, _key = TableWrite.create_user(
                conn, email=email, password_hash="x", handle=None
            )
            TableWrite.set_auto_redeem(conn, user_id, opted_in)
            user = TableRead.get_user_by_userid(conn, user_id)
        assert user is not None
        return user

    def holder(self, market, holds=None, *, opted_in=True):
        """A trader on `market` holding `holds` (default: `_ONE_USD` of the winner)."""
        user = self.account(opted_in=opted_in)
        with self.db.write() as conn:
            conn.execute(
                "INSERT INTO trades (TRADE_ID, ASSET_ID, TAKER_API_KEY, "
                "MAKER_API_KEY, STATUS, MATCH_TIME) VALUES (%s, %s, %s, %s, "
                "'MATCHED', 1)",
                (secrets.token_hex(8), market.yes, user.api_key, user.api_key),
            )
        holds = {market.yes: _ONE_USD} if holds is None else holds
        for token, amount in holds.items():
            self.chain.hold(user.eth_address, token, amount)
        return user

    def holders(self, market: _Market, n: int, exc: Exception) -> list[User]:
        """`n` owed holders on `market` whose claims raise `exc`."""
        users = [self.holder(market) for _ in range(n)]
        for user in users:
            self.outcomes[(user.user_id, market.id)] = exc
        return users

    def failing_claim(self, exc: Exception) -> tuple[_Market, tuple[str, int]]:
        """A market with one owed holder whose claim raises `exc`, and the claim key."""
        market = self.market()
        (user,) = self.holders(market, 1, exc)
        return market, (user.user_id, market.id)

    def markets(self, n: int) -> list[_Market]:
        """`n` markets in the order a pass takes them: newest first."""
        return [self.market() for _ in range(n)][::-1]

    def owed_markets(self, n: int) -> list[_Market]:
        markets = self.markets(n)
        for market in markets:
            self.holder(market)
        return markets

    def pending(self, user: User, market: _Market, kind="REDEEM", **details):
        """A transaction of theirs on `market` that went out, outcome unseen."""
        old = int(time.time()) - 30
        with self.db.write() as conn:
            TableWrite.insert_pending_user_tx(
                conn, _H1, user.api_key, kind, market.id, details, created_at=old
            )

    def mined(self, user: User, **receipt) -> None:
        self.chain.receipts[_H1] = {"status": 1, "from": user.eth_address, **receipt}

    def history(self, user: User) -> list[tuple[str, dict]]:
        with self.db.read() as conn:
            rows = conn.execute(
                "SELECT TRANSACTION_TYPE, DETAILS FROM transactions "
                "WHERE API_KEY = %s ORDER BY TRANSACTION_ID",
                (user.api_key,),
            ).fetchall()
        return [(r["TRANSACTION_TYPE"], json.loads(r["DETAILS"])) for r in rows]

    def run(self, *, cap: int = 20, settings: Settings | None = None) -> int:
        settings = settings or _settings(cap=cap)
        return redeem_resolved_markets(self.db, self.chain, settings)  # type: ignore[arg-type]

    def flagged(self, market: _Market) -> bool:
        with self.db.read() as conn:
            row = TableRead.read_market(conn, market.id)
        assert row is not None
        return row.fully_redeemed


@pytest.fixture(autouse=True)
def sponsors(monkeypatch) -> list[Settings]:
    """Swap `UserGasSponsor` for a recorder of the settings each pass built it from."""
    built: list[Settings] = []

    class _Sponsor:
        def __init__(self, db, onchain, settings):
            built.append(settings)
            self.min_claim_micro = settings.min_claim_micro

    monkeypatch.setattr(market_service, "UserGasSponsor", _Sponsor)
    return built


@pytest.fixture()
def world(monkeypatch) -> _World:
    world = _World(_Chain())
    # A bound method is not rebound on the instance, so `redeem` gets no service.
    monkeypatch.setattr(PositionService, "redeem", world.redeem)
    return world


@pytest.fixture()
def logs(caplog):
    """A callable returning what the pass has logged so far."""
    caplog.set_level(logging.INFO, logger=_LOGGER)
    return lambda: [r for r in caplog.records if r.name == _LOGGER]


_NONE, _LOSING, _DUST = (0, 0), (0, 50 * _ONE_USD), (9_999, 0)  # (yes, no) held


@pytest.mark.parametrize(
    ("minimum", "balances", "claims"),
    [
        pytest.param(10_000, [_NONE, _LOSING, _DUST], 0, id="dust"),
        pytest.param(0, [_NONE, _LOSING], 0, id="zero-minimum"),
        pytest.param(10_000, [(10_000, 0)], 1, id="exact-minimum"),
    ],
)
def test_claims_need_a_payout_of_the_minimum(world, minimum, balances, claims):
    """Nothing, the losing side or dust is pure admin gas: no claim, market done."""
    market = world.market()
    users = [world.holder(market, {market.yes: y, market.no: n}) for y, n in balances]
    # `Settings` refuses a 0 minimum, but the scan must not lean on that:
    # `model_copy` skips validation.
    settings = _settings().model_copy(update={"min_claim_micro": minimum})

    assert world.run(settings=settings) == claims
    # With a claim, it is the one holder's, with the vector; otherwise none.
    assert world.calls == [(users[0].user_id, market.id, (1, [1, 0]))][:claims]
    assert world.flagged(market) is True


def test_an_opted_out_holder_who_is_owed_still_holds_the_market_open(world):
    """The flag only stops the scan, and they may switch the toggle back on."""
    market = world.market()
    world.holder(market, opted_in=False)

    assert world.run() == 0
    assert world.calls == []
    assert world.flagged(market) is False


def test_a_pass_reads_the_vector_once_and_builds_the_sponsor_once(world, sponsors):
    """On SKALE a chain read costs ~0.5 s."""
    market = world.market()
    for _ in range(3):
        world.holder(market)
    settings = _settings()

    assert world.run(settings=settings) == 3
    assert world.chain.vector_reads == 1
    assert world.chain.balance_reads == 3
    assert [c.vector for c in world.calls] == [(1, [1, 0])] * 3
    assert sponsors == [settings]  # built from the settings the pass was given


def test_a_market_with_no_payout_on_chain_stays_open_and_unread(world):
    """RESOLVED in the database with no reportPayouts on chain (admin resolve route)."""
    world.chain.vector = (0, [0, 0])
    market = world.market()
    world.holder(market)

    assert world.run() == 0
    assert world.calls == []
    assert world.chain.balance_reads == 0
    assert world.flagged(market) is False


def test_a_market_nobody_traded_is_settled_without_a_chain_read(world):
    market = world.market()

    assert world.run() == 0
    assert world.chain.vector_reads == 0
    assert world.flagged(market) is True


def test_the_pass_stops_at_the_cap_and_the_next_pass_takes_the_rest(world):
    """Newest first; markets the cap missed stay open for the next pass."""
    markets = world.owed_markets(3)

    assert world.run(cap=2) == 2
    assert world.claimed_markets == [markets[0].id, markets[1].id]
    assert [world.flagged(m) for m in markets] == [True, True, False]

    assert world.run(cap=2) == 1
    assert world.calls[-1].market_id == markets[2].id
    assert world.flagged(markets[2]) is True


def test_a_failed_claim_counts_toward_the_cap(world):
    """Attempts, not successes: a refused claim spent reads, a revert spent gas."""
    (second,) = world.owed_markets(1)
    first, _ = world.failing_claim(RuntimeError("node hiccup"))

    assert world.run(cap=1) == 0
    assert world.claimed_markets == [first.id]
    assert world.flagged(second) is False


@pytest.mark.parametrize(
    ("exc", "level"),
    [
        pytest.param(errors.AdminGasPausedError(), "WARNING", id="breaker-paused"),
        pytest.param(errors.InsufficientGasError("dry"), "WARNING", id="not-sponsored"),
        pytest.param(errors.GasTopUpTimeoutError(), "WARNING", id="top-up-timed-out"),
        pytest.param(errors.GasPriceMovedError(), "WARNING", id="gas-price-moved"),
        pytest.param(errors.TransactionPendingError(), "WARNING", id="outcome-unknown"),
        pytest.param(RuntimeError("boom"), "ERROR", id="unexpected"),
    ],
)
def test_a_refused_claim_keeps_the_market_open_and_logs_by_kind(
    world, logs, exc, level
):
    """Refusals log no traceback (the redeem loop runs every 20 s); only a bug has one."""
    market, _ = world.failing_claim(exc)

    assert world.run() == 0
    assert world.flagged(market) is False
    records = logs()
    assert [r.levelname for r in records] == [level]
    assert (records[0].exc_info is not None) is (level == "ERROR")


@pytest.mark.parametrize(
    ("exc", "window"),
    [
        pytest.param(errors.AdminGasPausedError(), _REFUSED, id="breaker-paused"),
        pytest.param(errors.InsufficientGasError("dry"), _REFUSED, id="not-sponsored"),
        pytest.param(errors.GasTopUpTimeoutError(), _REFUSED, id="top-up-timed-out"),
        pytest.param(RuntimeError("boom"), _REFUSED, id="unexpected"),
        pytest.param(
            errors.TransactionRevertedError("reverted"), _REVERT, id="reverted"
        ),
        # Nothing failed (a hand claim in progress, or one that may mine yet).
        pytest.param(errors.TransactionInProgressError(), None, id="lock-held"),
        pytest.param(errors.TransactionPendingError(), None, id="outcome-unknown"),
    ],
)
def test_backoff_follows_a_failed_claim_not_a_busy_one(world, exc, window):
    """Short for what may clear soon; an hour for a revert, which costs gas each try."""
    market, key = world.failing_claim(exc)

    assert world.run() == 0
    if window is None:
        assert key not in _BACKOFF
    else:
        assert window - 10 < _BACKOFF[key] - time.monotonic() <= window
        # Inside the window: not tried again, and the market stays open.
        assert world.run() == 0
        assert len(world.calls) == 1
        assert world.flagged(market) is False
        _BACKOFF[key] = 0.0  # the window is up

    # The cause is gone: claimed, settled, forgotten.
    del world.outcomes[key]
    assert world.run() == 1
    assert len(world.calls) == 2
    assert world.flagged(market) is True
    assert key not in _BACKOFF


def test_a_refusal_is_forgiven_sooner_than_a_revert():
    assert 0 < _REFUSED < 3_600
    assert _REVERT == 3_600


def test_refused_holders_are_backed_off_so_the_next_pass_reaches_the_rest(world):
    """Unremembered refusals burn the cap every pass and starve every later claim."""
    first, second = world.markets(2)
    world.holders(first, 25, errors.AdminGasPausedError())
    last = world.holder(second)

    # Pass 1 spends the cap on 20 refusals and stops inside the first market.
    assert world.run() == 0
    tried = world.claimed_users
    assert len(tried) == 20

    # Pass 2 passes those 20 by without counting them: the other 5 are tried,
    # and so is the next market.
    assert world.run() == 1
    again = world.claimed_users[20:]
    assert len(again) == 6
    assert not set(again) & set(tried)
    assert last.user_id in again
    assert world.flagged(first) is False  # all 25 are still owed
    assert world.flagged(second) is True


def test_holders_with_held_locks_do_not_use_up_the_cap(world):
    """A held lock costs nothing and is not backed off: `cap` of them cannot starve."""
    first, second = world.markets(2)
    world.holders(first, 25, errors.TransactionInProgressError())
    honest = world.holder(second)

    # Claimed in the very first pass, after 25 refusals that spent none of the cap.
    assert world.run() == 1
    assert world.claimed_users.count(honest.user_id) == 1
    assert len(world.calls) == 26
    assert world.flagged(second) is True
    # Their market stays open and nobody is backed off.
    assert world.flagged(first) is False
    assert not _BACKOFF

    # Later passes meet the same busy locks again, and settle nothing more.
    for _ in range(4):
        assert world.run() == 0
    assert world.claimed_users.count(honest.user_id) == 1
    assert world.flagged(first) is False


def test_the_cap_still_counts_the_claims_that_were_tried_after_busy_locks(world):
    """Skipping a busy holder gives the cap back, no more."""
    # One market apiece, so a claimed holder is not met again by the next pass.
    honest = world.owed_markets(3)
    first = world.market()
    world.holders(first, 3, errors.TransactionInProgressError())

    assert world.run(cap=2) == 2
    claimed = [m for m in world.claimed_markets if m != first.id]
    assert claimed == [honest[0].id, honest[1].id]
    assert [world.flagged(m) for m in honest] == [True, True, False]

    assert world.run(cap=2) == 1
    assert world.calls[-1].market_id == honest[2].id
    assert world.flagged(honest[2]) is True
    assert world.flagged(first) is False


def test_really_held_locks_are_passed_by_before_any_chain_read(world, monkeypatch):
    """The same with the real sponsor lock: `redeem` refuses at `locked()`, no read."""
    monkeypatch.setattr(market_service, "UserGasSponsor", _REAL_SPONSOR)
    first, second = world.markets(2)
    squatters = [world.holder(first) for _ in range(25)]
    honest = world.holder(second)
    busy = {s.user_id for s in squatters}

    def redeem(service, user, market_id, *, payout_vector=None):
        if user.user_id in busy:
            return _REAL_REDEEM(service, user, market_id, payout_vector=payout_vector)
        world.redeem(user, market_id, payout_vector=payout_vector)

    monkeypatch.setattr(PositionService, "redeem", redeem)
    locks = [gas_sponsor._lock_for(s.eth_address) for s in squatters]  # noqa: SLF001
    for lock in locks:
        assert lock.acquire(blocking=False)
    try:
        assert world.run() == 1
    finally:
        for lock in locks:
            lock.release()

    assert world.claimed_users == [honest.user_id]
    assert world.flagged(second) is True
    assert world.flagged(first) is False
    assert not _BACKOFF
    # Only the pass's own reads: one vector a market, one balance a holder.
    assert world.chain.vector_reads == 2
    assert world.chain.balance_reads == 26


def test_a_market_that_cannot_be_read_is_skipped_and_the_pass_goes_on(world, logs):
    """A bad row (`ValueError: invalid literal for int() ... 'cut-y'`) is logged."""
    (good,) = world.owed_markets(1)
    poison = world.market(("cut-y", "cut-n"))  # the newest, so first
    world.holder(poison, {})

    assert world.run() == 1
    assert world.claimed_markets == [good.id]
    assert world.flagged(poison) is False
    assert world.flagged(good) is True
    logged = [r for r in logs() if r.levelno >= logging.ERROR]
    assert len(logged) == 1 and logged[0].exc_info is not None


@pytest.mark.parametrize("reads", ["vector_errors", "balance_errors"])
def test_an_rpc_error_on_one_read_skips_that_market_only(world, reads):
    first, second = world.owed_markets(2)
    # The first market's payout read, or its holder's balance read.
    setattr(world.chain, reads, [RuntimeError("node hiccup")])

    assert world.run() == 1
    assert world.claimed_markets == [second.id]
    assert world.flagged(first) is False
    assert world.flagged(second) is True


def test_a_holder_who_claimed_by_hand_meanwhile_does_not_hold_the_market_open(world):
    """The gate, re-reading under the holder's lock, saw them claim after the scan."""
    market, _ = world.failing_claim(errors.NothingToClaimError())

    assert world.run() == 0
    assert world.flagged(market) is True


def test_the_pass_settles_a_claim_that_mined_unseen_before_it_scans(world):
    """The request gave up on the receipt but the claim mined: settled first, done."""
    world.chain = _ReceiptChain()
    market = world.market()
    user = world.holder(market, {})  # the claim burned the tokens
    world.pending(user, market)
    world.mined(user, payout=_ONE_USD)

    assert world.run() == 0
    assert world.calls == []
    assert world.history(user) == [("REDEEM", {"collateral_amount": _ONE_USD})]
    assert world.flagged(market) is True


def test_a_split_that_mined_unseen_makes_its_holder_a_participant(world):
    """Settled first, the lost split is a SPLIT row: the same pass finds them owed."""
    world.chain = _ReceiptChain()
    market = world.market()
    user = world.account()
    world.chain.hold(user.eth_address, market.yes, _ONE_USD)
    world.pending(user, market, "SPLIT", amount=_ONE_USD)
    world.mined(user)

    assert world.run() == 1
    assert world.calls == [(user.user_id, market.id, (1, [1, 0]))]
    assert world.history(user) == [("SPLIT", {"amount": _ONE_USD})]


def test_a_holder_with_a_transaction_in_flight_is_skipped_and_not_counted(world):
    """Its claim would meet the duplicate guard's 409: skipped like a held lock."""
    world.chain = _ReceiptChain()
    first, second = world.markets(2)
    busy = world.holder(first)
    world.pending(busy, first)  # no receipt yet
    honest = world.holder(second)

    assert world.run(cap=1) == 1
    assert world.claimed_users == [honest.user_id]
    assert (busy.user_id, first.id) not in _BACKOFF
    assert world.flagged(first) is False
    assert world.flagged(second) is True


def test_a_split_in_flight_holds_the_market_open_for_a_holder_with_no_trades(world):
    """Not a participant yet, but the tokens may be on their way."""
    world.chain = _ReceiptChain()
    market = world.market()
    world.pending(world.account(), market, "SPLIT", amount=_ONE_USD)

    assert world.run() == 0
    assert world.calls == []
    assert world.flagged(market) is False


def test_a_failing_reconciler_does_not_stop_the_pass(world, logs, monkeypatch):
    world.owed_markets(1)

    def broken(_db, _admin):
        raise RuntimeError("database gone")

    monkeypatch.setattr(market_service, "reconcile_pending_user_txs", broken)

    assert world.run() == 1
    assert len(world.calls) == 1
    logged = [r for r in logs() if r.levelno >= logging.ERROR]
    assert len(logged) == 1 and logged[0].exc_info is not None
