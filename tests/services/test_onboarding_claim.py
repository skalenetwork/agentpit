import threading
import time

import pytest

from agentpit.auth.jwt import JwtCoder
from agentpit.config import Settings
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import AdminGasPausedError, OnboardingError
from agentpit.services.auth_service import AuthService
from tests.db_helpers import fresh_test_db


class _Chain:
    deployment_id = "0xctf"

    def __init__(self, fail=None, gate: threading.Event | None = None):
        self.funded: list[str] = []
        self._fail = fail
        self._gate = gate

    def fund_gas(self, address, _wei, *, timeout=30):
        if self._gate is not None:
            self._gate.wait(5)
        if self._fail is not None:
            raise self._fail
        self.funded.append(address)

    def faucet_drip(self, _address, *, timeout=30):
        pass

    def grant_user_approvals(self, _acct, *, timeout=30):
        pass

    def usd_balance(self, _address):
        return 100


def _service(chain):
    db = fresh_test_db()
    settings = Settings()
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
