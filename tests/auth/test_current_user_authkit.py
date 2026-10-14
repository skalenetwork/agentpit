"""Two credentials reach `current_user`, and this is the file that holds it.

`X-API-Key` is every bot trading today; the AuthKit access token is every
browser. The legacy `JwtCoder` bearer token was the third right up to the
cutover and is now refused -- `_CountingCoder` survives only to mint one and
prove that, and goes with `JwtCoder` in plan 4.

The dependency is driven directly rather than through the app because the
production key resolver fetches a JWKS over the network on the first token it
sees. Injecting a local resolver, the way `tests/auth/test_authkit_tokens.py`
does, is the only way to exercise this path offline.
"""
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from agentpit.auth.authkit_tokens import AuthKitVerifier, authkit_issuer
from agentpit.auth.dependencies import make_current_user_dep
from agentpit.auth.jwt import JwtCoder
from agentpit.config import Settings
from agentpit.db.table_write import TableWrite
from agentpit.services.agent_accounts import AgentAccounts
from tests.db_helpers import fresh_test_db

# The client id the token tests use. Every token below carries the claim set
# read off a REAL staging token on 2026-08-11: `iss` derived from the client
# id, a `client_id` claim, and no `aud`.
CLIENT_ID = "client_01KZRZ1QQXA15KX04VQBZPE0DZ"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _authkit_token(*, sub: str) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": authkit_issuer(CLIENT_ID),
            "sub": sub,
            "sid": "session_01",
            "jti": "01",
            "auth_time": now,
            "client_id": CLIENT_ID,
            "iat": now,
            # Measured, not chosen: an AuthKit access token lives 300 seconds.
            "exp": now + 300,
        },
        _KEY,
        algorithm="RS256",
    )


class _CountingCoder(JwtCoder):
    """The real coder, counting decodes so a test can prove it was NOT called."""

    def __init__(self) -> None:
        super().__init__(Settings())
        self.decodes = 0

    def decode(self, token: str):
        self.decodes += 1
        return super().decode(token)


class _CountingVerifier(AuthKitVerifier):
    """The real verifier over a local key: same contract, no JWKS fetch."""

    def __init__(self) -> None:
        super().__init__(
            client_id=CLIENT_ID, key_resolver=lambda _token: _KEY.public_key()
        )
        self.verifies = 0

    def verify(self, token: str) -> str:
        self.verifies += 1
        return super().verify(token)


@pytest.fixture
def db():
    return fresh_test_db()


@pytest.fixture
def accounts(db) -> AgentAccounts:
    return AgentAccounts(db, lambda *_: pytest.fail("an onboarded account must not onboard again"))


@pytest.fixture
def coder() -> _CountingCoder:
    return _CountingCoder()


@pytest.fixture
def verifier() -> _CountingVerifier:
    return _CountingVerifier()


@pytest.fixture
def current_user(verifier: _CountingVerifier):
    return make_current_user_dep(verifier)


def _bearer(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _make_user(db, *, email: str, workos_user_id: str | None = None):
    """A row, optionally already linked to a WorkOS identity. Returns
    (user_id, api_key)."""
    with db.write() as conn:
        user_id, _acct, api_key = TableWrite.create_user(
            conn, email=email, password_hash="$2b$12$x", handle=None
        )
        if workos_user_id is not None:
            TableWrite.set_workos_user_id(conn, user_id, workos_user_id)
    return user_id, api_key


def _user_count(db) -> int:
    with db.read() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]


def test_an_api_key_still_resolves_its_user_after_the_cutover(db, accounts):
    # The single most important assertion in this plan. Every bot trading
    # today authenticates this way and nothing about it moved to WorkOS.
    with db.write() as conn:
        user_id, _acct, api_key = TableWrite.create_user(
            conn, email="bot@example.com", password_hash=None, handle=None
        )
        TableWrite.mark_user_onboarded(conn, user_id)
    verifier = _CountingVerifier()

    dep = make_current_user_dep(verifier)
    user = dep(api_key=api_key, creds=None, db=db, accounts=accounts)

    assert user.user_id == user_id
    # Never consulted: the API-key path returns before the token path.
    assert verifier.verifies == 0


def test_a_bad_api_key_is_401_and_does_not_fall_through_to_the_token_path(
    db, accounts, verifier, current_user
):
    # Same short-circuit, on the failing side: a wrong key must be rejected
    # here, not handed to the verifier as if it were a bearer token.
    with pytest.raises(HTTPException) as raised:
        current_user("not-a-key", None, db, accounts)

    assert raised.value.status_code == 401
    assert verifier.verifies == 0


def test_a_legacy_jwt_is_no_longer_accepted(db, accounts, coder):
    # The cutover itself. Every browser holding one of these is signed out
    # exactly once -- which is what the product already does to everybody every
    # 24 hours, so the disruption is one it inflicts daily already.
    with db.write() as conn:
        user_id, _acct, _key = TableWrite.create_user(
            conn, email="legacy@example.com", password_hash="$2b$12$x", handle=None
        )
    token = coder.encode(user_id=user_id, email="legacy@example.com")

    dep = make_current_user_dep(_CountingVerifier())
    with pytest.raises(HTTPException) as excinfo:
        dep(
            api_key=None,
            creds=HTTPAuthorizationCredentials(
                scheme="Bearer", credentials=token
            ),
            db=db,
            accounts=accounts,
        )
    assert excinfo.value.status_code == 401


def test_an_authkit_token_resolves_the_row_its_sub_is_linked_to(db, accounts, current_user):
    user_id, _api_key = _make_user(
        db, email="new@example.com", workos_user_id="user_authkit_1"
    )

    resolved = current_user(None, _bearer(_authkit_token(sub="user_authkit_1")), db, accounts)

    assert resolved.user_id == user_id


def test_an_authkit_token_for_an_unknown_sub_is_401_and_creates_nothing(
    db, accounts, current_user
):
    # The token is perfectly valid -- right signature, right application, not
    # expired -- and still must not mint an account. Creation belongs to
    # POST /auth/session alone, which has proof the caller owns the address
    # (the mailed code). Creating here would give any valid AuthKit session
    # for our application a funded wallet by touching any authenticated route.
    before = _user_count(db)

    with pytest.raises(HTTPException) as raised:
        current_user(None, _bearer(_authkit_token(sub="user_nobody")), db, accounts)

    assert raised.value.status_code == 401
    assert _user_count(db) == before


def test_a_garbage_bearer_token_is_401(db, accounts, current_user):
    with pytest.raises(HTTPException) as raised:
        current_user(None, _bearer("not.a.real.jwt"), db, accounts)

    assert raised.value.status_code == 401


def test_an_authkit_token_is_401_where_no_verifier_was_configured(db, accounts):
    # A deployment without WORKOS_CLIENT_ID gets `authkit=None`. Such a
    # deployment can authenticate bots by X-API-Key and nobody else, which is
    # a refusal rather than the crash on the None it must not be.
    unconfigured = make_current_user_dep(None)
    _make_user(db, email="orphan@example.com", workos_user_id="user_authkit_2")

    with pytest.raises(HTTPException) as raised:
        unconfigured(None, _bearer(_authkit_token(sub="user_authkit_2")), db, accounts)

    assert raised.value.status_code == 401


def test_the_app_factory_passes_a_verifier_into_the_dependency():
    """The AuthKit branch is dead in production unless `create_app` wires it.

    Every test above builds its own dependency, so all of them stay green if
    `create_app` stops passing the verifier -- while every AuthKit session in
    production 401s as "invalid token". Read the closure rather than send a
    token, because the verifier a real app builds resolves keys over the
    network and no test may.
    """
    import inspect

    from agentpit.api import deps
    from agentpit.api.app import create_app

    def _wired(app):
        dep = app.dependency_overrides[deps.get_current_user]
        return inspect.getclosurevars(dep).nonlocals["authkit"]

    assert isinstance(
        _wired(create_app(Settings(workos_client_id="client_wiring"))),
        AuthKitVerifier,
    )
    # And a deployment with no client id gets None through the same wire --
    # the issuer and the JWKS URL are both derived from it, so a verifier
    # built without one could only reject everything.
    assert _wired(create_app(Settings(workos_client_id=""))) is None
