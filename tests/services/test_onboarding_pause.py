import pytest

from agentpit.auth.jwt import JwtCoder
from agentpit.config import Settings
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import AdminGasPausedError
from agentpit.services.auth_service import AuthService
from tests.db_helpers import fresh_test_db


class _PausedChain:
    def fund_gas(self, *_a, **_k):
        raise AdminGasPausedError()


def test_onboarding_surfaces_the_pause_not_an_onboarding_error():
    db = fresh_test_db()
    settings = Settings()
    service = AuthService(db, JwtCoder(settings), _PausedChain(), settings)  # type: ignore[arg-type]
    with db.write() as conn:
        user_id, acct, _ = TableWrite.create_user(conn, email="pause@example.com", password_hash=None, handle=None)
    with pytest.raises(AdminGasPausedError):
        service._onboard_new_account(user_id, acct)
