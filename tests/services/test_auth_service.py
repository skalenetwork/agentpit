"""The legacy sign-in path, which is now reachable only by reverting.

`/register`, `/login` and `/auth/google` were deleted in the cutover, but the
service behind them was deliberately left in the tree: not deleting it IS the
rollback plan, and reverting the cutover commit puts these methods straight
back under live routes.

That is why this file exists. Reverting works today only because the revert
restores code and tests together; the exposure is temporal. Between now and
plan 4, a change to `TableRead`, `TableWrite`, `AuthResponse` or the password
helpers can rot this path while the whole suite stays green, and it would
surface at the moment somebody reverts under pressure -- which is exactly when
nobody wants to discover it. A rollback path with no test is a rollback path
nobody has checked.

Deliberately small, and deliberately at the service rather than the HTTP layer
since the route is gone now: one success, one refusal. It exists to fail
loudly if the legacy path rots, not to re-cover what the deleted endpoint
tests covered.
"""
import pytest

from agentpit.auth.jwt import JwtCoder
from agentpit.auth.passwords import hash_password
from agentpit.config import Settings
from agentpit.datastructures.login_request import LoginRequest
from agentpit.db.session import DbSession
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import InvalidCredentialsError
from agentpit.services.auth_service import AuthService


class _UntouchedChain:
    """`login` calls `_maybe_reonboard`, which returns on `onboarded_at is
    None` before it would reach the chain. These rows are never onboarded, so
    nothing here should ever run -- and if the guard order changes, these
    tests say so instead of quietly taking a second of anvil round-trips."""

    def transaction_count(self, *_args, **_kwargs):
        raise AssertionError("login walked the chain for an un-onboarded row")

    def native_balance(self, *_args, **_kwargs):
        raise AssertionError("login walked the chain for an un-onboarded row")

    def fund_gas(self, *_args, **_kwargs):
        raise AssertionError("login topped up gas")

    def faucet_drip(self, *_args, **_kwargs):
        raise AssertionError("login dripped collateral")

    def approval_calls(self, *_args, **_kwargs):
        raise AssertionError("login built the approvals")

    def send_as_user(self, *_args, **_kwargs):
        raise AssertionError("login signed as the user")


def _service() -> tuple[AuthService, DbSession]:
    settings = Settings()
    db = DbSession(settings.database_url)
    return AuthService(db, JwtCoder(settings), _UntouchedChain(), settings), db


def _seed(db: DbSession, *, email: str, password: str) -> str:
    with db.write() as conn:
        user_id, _acct, _api_key = TableWrite.create_user(
            conn, email=email, password_hash=hash_password(password), handle=None
        )
    return user_id


def test_login_still_signs_in_an_account_that_has_a_password():
    service, db = _service()
    password = "hunter22hunter22"
    user_id = _seed(db, email="rollback@example.com", password=password)

    resp = service.login(LoginRequest(email="rollback@example.com", password=password))

    assert resp.access_token
    assert resp.user.user_id == user_id
    assert resp.user.email == "rollback@example.com"


def test_login_still_refuses_the_wrong_password():
    service, db = _service()
    _seed(db, email="rollback2@example.com", password="hunter22hunter22")

    with pytest.raises(InvalidCredentialsError):
        service.login(
            LoginRequest(email="rollback2@example.com", password="nope-nope-nope")
        )
