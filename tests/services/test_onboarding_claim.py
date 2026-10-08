import threading
import time

import pytest

from agentpit.auth.jwt import JwtCoder
from agentpit.config import Settings
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import (
    AdminGasPausedError,
    InsufficientGasError,
    OnboardingError,
)
from agentpit.onchain.tx_sender import TRANSFER_GAS
from agentpit.services.auth_service import AuthService
from agentpit.services.gas_sponsor import UserGasSponsor
from tests.db_helpers import fresh_test_db
from tests.onboarding_fakes import (
    APPROVAL_GAS,
    APPROVAL_LIMIT,
    GAS_PRICE,
    ONBOARDING_NEED,
    OnboardingChain,
)


class _Chain(OnboardingChain):
    """The wallet's top-up can be made to fail, or to wait on a gate."""

    def __init__(self, fail=None, gate: threading.Event | None = None):
        super().__init__()
        self._fail = fail
        self._gate = gate

    def fund_gas(self, user_address, value_wei, *, timeout=30):
        if self._gate is not None:
            self._gate.wait(5)
        if self._fail is not None:
            raise self._fail
        return super().fund_gas(user_address, value_wei, timeout=timeout)


class _CannotPay(_Chain):
    """The sponsor's last word on a wallet the node keeps refusing (its 402)."""

    def send_as_user(self, *_a, **_k):
        raise InsufficientGasError("this account's wallet cannot pay for the transaction")


def _service(chain, settings: Settings | None = None):
    db = fresh_test_db()
    settings = settings or Settings()
    return AuthService(db, JwtCoder(settings), chain, settings), db  # type: ignore[arg-type]


def _row(db, email):
    with db.write() as conn:
        user_id, acct, _ = TableWrite.create_user(conn, email=email, password_hash=None, handle=None)
    return user_id, acct


def _claimed_at(db, user_id):
    with db.read() as conn:
        return conn.execute("SELECT ONBOARDING_STARTED_AT AS S FROM users WHERE USER_ID = %s", (user_id,)).fetchone()["S"]


def test_onboards_once_and_a_repeat_is_free():
    chain = _Chain()
    service, db = _service(chain)
    user_id, acct = _row(db, "once@example.com")
    assert service._onboard_new_account(user_id, acct).onboarded_at is not None
    assert service._onboard_new_account(user_id, acct).onboarded_at is not None
    assert len(chain.funded) == 1


def test_a_held_claim_refuses_a_second_onboarding():
    chain = _Chain()
    service, db = _service(chain)
    user_id, acct = _row(db, "held@example.com")
    with db.write() as conn:
        conn.execute("UPDATE users SET ONBOARDING_STARTED_AT = %s WHERE USER_ID = %s", (int(time.time()), user_id))
    with pytest.raises(OnboardingError, match="already being set up"):
        service._onboard_new_account(user_id, acct)
    assert chain.funded == []


def test_a_stale_claim_is_taken_over():
    chain = _Chain()
    service, db = _service(chain)
    user_id, acct = _row(db, "stale@example.com")
    with db.write() as conn:
        conn.execute("UPDATE users SET ONBOARDING_STARTED_AT = %s WHERE USER_ID = %s",
                     (int(time.time()) - AuthService.ONBOARDING_CLAIM_STALE_S - 1, user_id))
    service._onboard_new_account(user_id, acct)
    assert len(chain.funded) == 1


def test_failed_onboarding_releases_the_claim():
    service, db = _service(_Chain(fail=RuntimeError("chain down")))
    user_id, acct = _row(db, "retry@example.com")
    with pytest.raises(OnboardingError):
        service._onboard_new_account(user_id, acct)
    assert _claimed_at(db, user_id) is None
    good = _Chain()
    service._onchain = good           # the honest retry a second later
    service._onboard_new_account(user_id, acct)
    assert len(good.funded) == 1


def test_a_paused_breaker_releases_the_claim():
    service, db = _service(_Chain(fail=AdminGasPausedError()))
    user_id, acct = _row(db, "paused@example.com")
    with pytest.raises(AdminGasPausedError):
        service._onboard_new_account(user_id, acct)
    assert _claimed_at(db, user_id) is None


def test_parallel_first_sign_ins_fund_once():
    gate = threading.Event()
    chain = _Chain(gate=gate)
    service, db = _service(chain)
    user_id, acct = _row(db, "race@example.com")
    errors: list[Exception] = []

    def run():
        try:
            service._onboard_new_account(user_id, acct)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    time.sleep(0.3)
    gate.set()
    for t in threads:
        t.join(10)
    assert len(chain.funded) == 1
    assert len(errors) == 1 and isinstance(errors[0], OnboardingError)


def test_clear_user_onboarded_also_clears_the_claim():
    service, db = _service(_Chain())
    user_id, acct = _row(db, "clear@example.com")
    service._onboard_new_account(user_id, acct)
    with db.write() as conn:
        TableWrite.clear_user_onboarded(conn, user_id)
    assert _claimed_at(db, user_id) is None


def test_onboarding_tops_up_exactly_the_approvals_and_signs_them_as_the_user():
    chain = _Chain()
    service, db = _service(chain)
    user_id, acct = _row(db, "exact@example.com")

    service._onboard_new_account(user_id, acct)

    # The drip, one top-up to exactly three approvals' limits, the approvals.
    assert chain.calls == ["faucet_drip", "fund_gas"] + ["send_as_user"] * 3
    assert chain.funded == [(acct.address, ONBOARDING_NEED)]
    assert chain.sent == [
        (acct.address, fn, APPROVAL_LIMIT, GAS_PRICE) for fn in chain.approval_calls()
    ]


def test_onboarding_is_sponsored_with_the_user_gas_switch_off():
    """The switch stops claims, splits and merges from being funded, never
    signups: without the top-up no account or agent could be created."""
    chain = _Chain()
    service, db = _service(chain, Settings(AGENTPIT_SPONSOR_USER_GAS=False))
    user_id, acct = _row(db, "switch@example.com")

    assert service._onboard_new_account(user_id, acct).onboarded_at is not None
    assert chain.funded == [(acct.address, ONBOARDING_NEED)]


def test_onboarding_gas_is_booked_to_the_account():
    chain = _Chain()
    service, db = _service(chain)
    user_id, acct = _row(db, "booked@example.com")
    day = int(time.time()) // 86_400

    user = service._onboard_new_account(user_id, acct)

    with db.read() as conn:
        assert TableRead.sponsored_gas_used(conn, user.api_key, day) == TRANSFER_GAS + 3 * APPROVAL_GAS


def test_a_transaction_in_progress_reads_as_a_lost_claim():
    chain = _Chain()
    service, db = _service(chain)
    user_id, acct = _row(db, "busy@example.com")
    with db.read() as conn:
        user = TableRead.get_user_by_userid(conn, user_id)
    assert user is not None

    with UserGasSponsor(db, chain, Settings()).locked(user):  # type: ignore[arg-type]
        with pytest.raises(OnboardingError, match="already being set up"):
            service._onboard_new_account(user_id, acct)

    assert chain.calls == []                 # refused before the admin sent anything
    assert _claimed_at(db, user_id) is None
    assert service._onboard_new_account(user_id, acct).onboarded_at is not None


def test_a_wallet_that_cannot_pay_is_a_402_and_releases_the_claim():
    service, db = _service(_CannotPay())
    user_id, acct = _row(db, "dry@example.com")
    with pytest.raises(InsufficientGasError):
        service._onboard_new_account(user_id, acct)
    assert _claimed_at(db, user_id) is None
