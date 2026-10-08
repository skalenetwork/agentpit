import logging

import pytest
from web3.exceptions import TimeExhausted

from agentpit.auth.jwt import JwtCoder
from agentpit.config import Settings
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import AdminGasPausedError, GasTopUpTimeoutError
from agentpit.services.auth_service import AuthService
from tests.db_helpers import fresh_test_db
from tests.onboarding_fakes import OnboardingChain


class _PausedChain(OnboardingChain):
    """The breaker is paused: the wallet's top-up, a sponsored send like any
    other, is refused."""

    def fund_gas(self, *_a, **_k):
        raise AdminGasPausedError()


def test_onboarding_surfaces_the_pause_not_an_onboarding_error():
    db = fresh_test_db()
    settings = Settings()
    chain = _PausedChain()
    service = AuthService(db, JwtCoder(settings), chain, settings)  # type: ignore[arg-type]
    with db.write() as conn:
        user_id, acct, _ = TableWrite.create_user(conn, email="pause@example.com", password_hash=None, handle=None)
    with pytest.raises(AdminGasPausedError):
        service._onboard_new_account(user_id, acct)
    assert "send_as_user" not in chain.calls  # nothing signed from a wallet that was never funded


class _BusyChain(OnboardingChain):
    """The wallet's top-up got no receipt in time (or no free admin slot)."""

    def fund_gas(self, *_a, **_k):
        raise TimeExhausted("no receipt for the top-up")


def test_onboarding_surfaces_a_top_up_timeout_not_an_onboarding_error(caplog):
    """A 503 "the platform is busy", like the pause, and no traceback per
    sign-in: the old catch-all made it a 400 `OnboardingError` and logged it
    with `log.exception`."""
    db = fresh_test_db()
    settings = Settings()
    chain = _BusyChain()
    service = AuthService(db, JwtCoder(settings), chain, settings)  # type: ignore[arg-type]
    with db.write() as conn:
        user_id, acct, _ = TableWrite.create_user(conn, email="busy@example.com", password_hash=None, handle=None)

    with caplog.at_level(logging.ERROR, logger="agentpit.services.auth_service"):
        with pytest.raises(GasTopUpTimeoutError):
            service._onboard_new_account(user_id, acct)

    assert "send_as_user" not in chain.calls
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    # The claim is back, so the retry they are told to make is not turned away.
    with db.read() as conn:
        row = conn.execute("SELECT ONBOARDING_STARTED_AT AS S FROM users WHERE USER_ID = %s", (user_id,)).fetchone()
    assert row["S"] is None
