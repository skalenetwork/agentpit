"""What one auto-redeem pass decides, with the chain faked.

Since gasless claims (2026-10-08) the admin tops up every claim the pass
makes, so who it claims for, how many it makes per pass, and what it does
when a claim cannot go out are questions of cost, not only of settlement.
The transaction under `PositionService.redeem` is covered by
tests/onchain/test_auto_redeem.py and the sponsor's own tests; here `redeem`
is a stub and the chain is two reads.
"""

from __future__ import annotations

import json
import logging
import secrets
import time

import pytest

from agentpit.config import Settings
from agentpit.datastructures.user import User
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import (
    AdminGasPausedError,
    InsufficientGasError,
    NothingToClaimError,
    TransactionInProgressError,
    TransactionRevertedError,
)
from agentpit.polymarket import polymarket_sync
from agentpit.polymarket.polymarket_sync import auto_redeem_resolved_markets
from agentpit.services import gas_sponsor
from agentpit.services.position_service import PositionService
from tests.db_helpers import fresh_test_db

_LOGGER = "agentpit.polymarket.polymarket_sync"
_ONE_USD = 1_000_000  # micro-apUSD, well above the $0.01 minimum


class _Chain:
    """The two reads a pass makes, and nothing else.

    Not a MagicMock on purpose: if the pass itself touched anything else
    (fund_gas, send_as_user, ...) it would raise here instead of quietly
    recording a call. Unset balances read 0.
    """

    def __init__(self, vector: tuple[int, list[int]] = (1, [1, 0])):
        self.vector = vector
        self.held: dict[str, dict[int, int]] = {}
        self.vector_reads = 0
        self.balance_reads = 0

    def hold(self, address: str, token: str, amount: int) -> None:
        self.held.setdefault(address.lower(), {})[int(token)] = amount

    def payout_vector(
        self, condition_id: bytes, outcome_count: int = 2
    ) -> tuple[int, list[int]]:
        self.vector_reads += 1
        return self.vector

    def ctf_balances(self, address: str, token_ids: list[int]) -> list[int]:
        self.balance_reads += 1
        held = self.held.get(address.lower(), {})
        return [held.get(t, 0) for t in token_ids]


@pytest.fixture(autouse=True)
def sponsors(monkeypatch) -> list[Settings]:
    """Replace `UserGasSponsor` with a recorder of the settings each pass
    built it from. `redeem` is stubbed, so nothing ever sends through it; the
    pass only reads its `min_claim_micro`."""
    built: list[Settings] = []

    class _Sponsor:
        def __init__(self, db, onchain, settings):
            built.append(settings)
            self.min_claim_micro = settings.min_claim_micro

    monkeypatch.setattr(gas_sponsor, "UserGasSponsor", _Sponsor)
    return built


@pytest.fixture()
def claims(monkeypatch):
    """Stub `PositionService.redeem`. Returns `(calls, outcomes)`: each call
    is recorded as `(user_id, market_id, payout_vector)`, and an exception
    set in `outcomes[(user_id, market_id)]` is raised instead of returning."""
    calls: list[tuple[str, int, tuple[int, list[int]] | None]] = []
    outcomes: dict[tuple[str, int], Exception] = {}

    def _redeem(self, user, market_id, *, payout_vector=None):
        calls.append((user.user_id, market_id, payout_vector))
        exc = outcomes.get((user.user_id, market_id))
        if exc is not None:
            raise exc

    monkeypatch.setattr(PositionService, "redeem", _redeem)
    return calls, outcomes


def _settings(*, cap: int = 20) -> Settings:
    return Settings(
        _env_file=None, min_claim_micro=10_000, auto_redeem_max_per_pass=cap
    )


def _market(db) -> tuple[int, str, str]:
    """A RESOLVED market whose YES (index 0) won: `(market_id, yes, no)`."""
    yes = str(int.from_bytes(secrets.token_bytes(8), "big"))
    no = str(int.from_bytes(secrets.token_bytes(8), "big"))
    with db.write() as conn:
        row = conn.execute(
            "INSERT INTO markets (CONDITION_ID, QUESTION, SLUG, DESCRIPTION, "
            "ERC1155_TOKENS, START_DATE, MARKET_STATE, RESOLVED_OUTCOME) "
            "VALUES (%s, %s, %s, 'd', %s, 100, 'RESOLVED', 0) "
            "RETURNING MARKET_ID",
            (
                f"0x{secrets.token_hex(32)}",
                f"Already won {secrets.token_hex(4)}?",
                f"already-won-{secrets.token_hex(4)}",
                json.dumps([[yes, "YES"], [no, "NO"]]),
            ),
        ).fetchone()
    return row["MARKET_ID"], yes, no


def _holder(
    db,
    chain: _Chain,
    market: tuple[int, str, str],
    *,
    holds: dict[str, int],
    auto_redeem: bool = True,
) -> User:
    """An account with a trade on `market` (which is how the pass finds it)
    holding `holds` (token -> balance) on the fake chain."""
    _market_id, yes, _no = market
    with db.write() as conn:
        user_id, acct, api_key = TableWrite.create_user(
            conn,
            email=f"holder-{secrets.token_hex(4)}@example.com",
            password_hash="x",
            handle=None,
        )
        TableWrite.set_auto_redeem(conn, user_id, auto_redeem)
        conn.execute(
            "INSERT INTO trades (TRADE_ID, ASSET_ID, TAKER_API_KEY, "
            "MAKER_API_KEY, STATUS, MATCH_TIME) VALUES (%s, %s, %s, %s, "
            "'MATCHED', 1)",
            (secrets.token_hex(8), yes, api_key, api_key),
        )
    for token, amount in holds.items():
        chain.hold(acct.address, token, amount)
    with db.read() as conn:
        user = TableRead.get_user_by_userid(conn, user_id)
    assert user is not None
    return user


def _flagged(db, market_id: int) -> bool:
    with db.read() as conn:
        market = TableRead.read_market(conn, market_id)
    assert market is not None
    return market.fully_redeemed


# ----- who is owed a claim ----------------------------------------------------


def test_holders_owed_less_than_the_minimum_cost_nothing_and_settle_the_market(
    claims,
):
    """Nothing, only the losing side, or dust under $0.01: a claim for any of
    them would be pure admin gas, so none is made -- and since nobody is owed
    one worth making, the market is done."""
    calls, _ = claims
    db, chain = fresh_test_db(), _Chain()
    market = _market(db)
    market_id, yes, no = market
    _holder(db, chain, market, holds={})
    _holder(db, chain, market, holds={no: 50 * _ONE_USD})
    _holder(db, chain, market, holds={yes: 9_999})

    assert auto_redeem_resolved_markets(db, chain, _settings()) == 0  # type: ignore[arg-type]
    assert calls == []
    assert _flagged(db, market_id) is True


def test_a_payout_of_exactly_the_minimum_is_claimed(claims):
    calls, _ = claims
    db, chain = fresh_test_db(), _Chain()
    market = _market(db)
    user = _holder(db, chain, market, holds={market[1]: 10_000})

    assert auto_redeem_resolved_markets(db, chain, _settings()) == 1  # type: ignore[arg-type]
    assert calls == [(user.user_id, market[0], (1, [1, 0]))]
    assert _flagged(db, market[0]) is True


def test_an_opted_out_holder_who_is_owed_still_holds_the_market_open(claims):
    """The flag only stops the scan, and they may switch the toggle back on."""
    calls, _ = claims
    db, chain = fresh_test_db(), _Chain()
    market = _market(db)
    _holder(db, chain, market, holds={market[1]: _ONE_USD}, auto_redeem=False)

    assert auto_redeem_resolved_markets(db, chain, _settings()) == 0  # type: ignore[arg-type]
    assert calls == []
    assert _flagged(db, market[0]) is False


def test_the_vector_is_read_once_per_market_and_handed_to_every_claim(claims):
    """One payout read per market, one balance read per holder: on SKALE a
    chain read costs ~0.5 s, and the pass holds `_redeem_lock` meanwhile."""
    calls, _ = claims
    db, chain = fresh_test_db(), _Chain()
    market = _market(db)
    for _ in range(3):
        _holder(db, chain, market, holds={market[1]: _ONE_USD})

    assert auto_redeem_resolved_markets(db, chain, _settings()) == 3  # type: ignore[arg-type]
    assert chain.vector_reads == 1
    assert chain.balance_reads == 3
    assert [vector for _user, _market, vector in calls] == [(1, [1, 0])] * 3


def test_a_market_with_no_payout_on_chain_stays_open_and_unread(claims):
    """RESOLVED in the database without a reportPayouts on chain (the admin
    resolve route): nobody can be paid yet, so nobody is settled either, and
    no balance is read for it."""
    calls, _ = claims
    db, chain = fresh_test_db(), _Chain(vector=(0, [0, 0]))
    market = _market(db)
    _holder(db, chain, market, holds={market[1]: _ONE_USD})

    assert auto_redeem_resolved_markets(db, chain, _settings()) == 0  # type: ignore[arg-type]
    assert calls == []
    assert chain.balance_reads == 0
    assert _flagged(db, market[0]) is False


def test_a_market_nobody_traded_is_settled_without_a_chain_read(claims):
    db, chain = fresh_test_db(), _Chain()
    market = _market(db)

    assert auto_redeem_resolved_markets(db, chain, _settings()) == 0  # type: ignore[arg-type]
    assert chain.vector_reads == 0
    assert _flagged(db, market[0]) is True


def test_the_sponsor_is_built_once_per_pass_from_the_settings_given(
    claims, sponsors
):
    db, chain = fresh_test_db(), _Chain()
    market = _market(db)
    _holder(db, chain, market, holds={market[1]: _ONE_USD})
    settings = _settings()

    auto_redeem_resolved_markets(db, chain, settings)  # type: ignore[arg-type]
    assert sponsors == [settings]


# ----- how many per pass ------------------------------------------------------


def test_the_pass_stops_at_the_cap_and_the_next_pass_takes_the_rest(claims):
    """Each claim is about two blocks under `_redeem_lock`, which both
    resolution loops wait on. Markets a capped pass did not reach stay open."""
    calls, _ = claims
    db, chain = fresh_test_db(), _Chain()
    markets = [_market(db) for _ in range(3)]
    for market in markets:
        _holder(db, chain, market, holds={market[1]: _ONE_USD})

    assert auto_redeem_resolved_markets(db, chain, _settings(cap=2)) == 2  # type: ignore[arg-type]
    assert [m for _user, m, _vector in calls] == [markets[0][0], markets[1][0]]
    assert [_flagged(db, m[0]) for m in markets] == [True, True, False]

    assert auto_redeem_resolved_markets(db, chain, _settings(cap=2)) == 1  # type: ignore[arg-type]
    assert calls[-1][1] == markets[2][0]
    assert _flagged(db, markets[2][0]) is True


def test_a_failed_claim_counts_toward_the_cap(claims):
    """Attempts, not successes: a refused claim already spent reads and an
    estimate under the lock, and a reverted one spent gas."""
    calls, outcomes = claims
    db, chain = fresh_test_db(), _Chain()
    first, second = _market(db), _market(db)
    user = _holder(db, chain, first, holds={first[1]: _ONE_USD})
    _holder(db, chain, second, holds={second[1]: _ONE_USD})
    outcomes[(user.user_id, first[0])] = RuntimeError("node hiccup")

    assert auto_redeem_resolved_markets(db, chain, _settings(cap=1)) == 0  # type: ignore[arg-type]
    assert [m for _user, m, _vector in calls] == [first[0]]
    assert _flagged(db, second[0]) is False


# ----- when a claim cannot go out --------------------------------------------


@pytest.mark.parametrize(
    ("exc", "level"),
    [
        (TransactionInProgressError(), logging.INFO),
        (AdminGasPausedError(), logging.WARNING),
        (InsufficientGasError("wallet balance too low"), logging.WARNING),
    ],
    ids=["lock-held", "breaker-paused", "not-sponsored"],
)
def test_an_expected_refusal_skips_the_holder_quietly_and_keeps_the_market_open(
    claims, caplog, exc, level
):
    """A held lock (they are claiming by hand), a paused gas breaker, or a dry
    wallet while sponsoring is switched off: retried next pass, and logged
    without a traceback. The pin loop runs a pass every 20 s; a traceback per
    holder each time would bury the failures that need one."""
    _calls, outcomes = claims
    db, chain = fresh_test_db(), _Chain()
    market = _market(db)
    user = _holder(db, chain, market, holds={market[1]: _ONE_USD})
    outcomes[(user.user_id, market[0])] = exc
    caplog.set_level(logging.INFO, logger=_LOGGER)

    assert auto_redeem_resolved_markets(db, chain, _settings()) == 0  # type: ignore[arg-type]
    assert _flagged(db, market[0]) is False
    records = [r for r in caplog.records if r.name == _LOGGER]
    assert [r.levelno for r in records] == [level]
    assert records[0].exc_info is None


def test_an_unexpected_error_keeps_its_traceback(claims, caplog):
    _calls, outcomes = claims
    db, chain = fresh_test_db(), _Chain()
    market = _market(db)
    user = _holder(db, chain, market, holds={market[1]: _ONE_USD})
    outcomes[(user.user_id, market[0])] = RuntimeError("boom")
    caplog.set_level(logging.INFO, logger=_LOGGER)

    assert auto_redeem_resolved_markets(db, chain, _settings()) == 0  # type: ignore[arg-type]
    assert _flagged(db, market[0]) is False
    errors = [
        r for r in caplog.records if r.name == _LOGGER and r.levelno == logging.ERROR
    ]
    assert len(errors) == 1
    assert errors[0].exc_info is not None


def test_a_reverted_claim_is_left_alone_for_an_hour(claims):
    """After the on-chain gate a revert should not happen. If it does, it must
    not repeat, and cost gas, on every pass."""
    calls, outcomes = claims
    db, chain = fresh_test_db(), _Chain()
    market = _market(db)
    user = _holder(db, chain, market, holds={market[1]: _ONE_USD})
    key = (user.user_id, market[0])
    outcomes[key] = TransactionRevertedError("redeemPositions reverted")

    assert auto_redeem_resolved_markets(db, chain, _settings()) == 0  # type: ignore[arg-type]
    assert len(calls) == 1
    left = polymarket_sync._revert_backoff_until[key] - time.monotonic()
    assert 3_590 < left <= 3_600

    # Inside the hour: not tried again, and the market stays open.
    assert auto_redeem_resolved_markets(db, chain, _settings()) == 0  # type: ignore[arg-type]
    assert len(calls) == 1
    assert _flagged(db, market[0]) is False

    # The hour is up and the cause is gone: claimed, settled, forgotten.
    polymarket_sync._revert_backoff_until[key] = 0.0
    del outcomes[key]
    assert auto_redeem_resolved_markets(db, chain, _settings()) == 1  # type: ignore[arg-type]
    assert len(calls) == 2
    assert _flagged(db, market[0]) is True
    assert key not in polymarket_sync._revert_backoff_until


def test_a_holder_who_claimed_by_hand_meanwhile_does_not_hold_the_market_open(
    claims,
):
    """The scan saw a payout; the gate, re-reading under the holder's lock a
    moment later, did not: they pressed Claim in between. Nothing is owed."""
    _calls, outcomes = claims
    db, chain = fresh_test_db(), _Chain()
    market = _market(db)
    user = _holder(db, chain, market, holds={market[1]: _ONE_USD})
    outcomes[(user.user_id, market[0])] = NothingToClaimError()

    assert auto_redeem_resolved_markets(db, chain, _settings()) == 0  # type: ignore[arg-type]
    assert _flagged(db, market[0]) is True
