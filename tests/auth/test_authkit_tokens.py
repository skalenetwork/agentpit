"""The token shape here is copied from a REAL staging token, not invented.

The previous version of this file minted tokens carrying whatever claims the
implementation expected, so it passed against an implementation that would have
rejected every genuine sign-in. Every token below therefore uses the claim set
observed on 2026-08-11: `iss` = api.workos.com/user_management/<client_id>, a
`client_id` claim, and NO `aud`.
"""
import threading
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from agentpit.auth.authkit_tokens import (
    AuthKitVerifier,
    authkit_issuer,
    authkit_jwks_url,
    cached_key_resolver,
    remote_jwks_resolver,
)
from agentpit.domain.exceptions import InvalidCredentialsError

CLIENT_ID = "client_01KZRZ1QQXA15KX04VQBZPE0DZ"
ISSUER = authkit_issuer(CLIENT_ID)

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _token(key=_KEY, *, sub="user_01", iss=ISSUER, client_id=CLIENT_ID,
           exp_delta=300, extra=None, drop=(), kid=None):
    now = int(time.time())
    claims = {
        "iss": iss,
        "sub": sub,
        "sid": "session_01",
        "jti": "01",
        "auth_time": now,
        "client_id": client_id,
        "iat": now,
        "exp": now + exp_delta,
    }
    claims.update(extra or {})
    for name in drop:
        claims.pop(name, None)
    headers = {"kid": kid} if kid is not None else None
    return jwt.encode(claims, key, algorithm="RS256", headers=headers)


class _CountingResolver:
    """A key resolver that stands in for the remote one and counts its calls.

    Counting rather than raising on purpose: `verify` turns every exception
    into `InvalidCredentialsError`, so a resolver that raised would make the
    "it was never consulted" assertions pass whether or not it was consulted.
    """

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, _token: str):
        self.calls += 1
        return _KEY.public_key()


def _verifier() -> AuthKitVerifier:
    # The resolver stands in for the remote JWKS: same contract, no network.
    return AuthKitVerifier(
        client_id=CLIENT_ID, key_resolver=lambda _token: _KEY.public_key()
    )


def test_issuer_is_derived_from_the_client_id_not_the_authkit_domain():
    # The mistake this whole module was rewritten for: the AuthKit domain
    # serves the sign-in surface and a JWKS, but it is NOT what `iss` says.
    assert ISSUER == (
        "https://api.workos.com/user_management/client_01KZRZ1QQXA15KX04VQBZPE0DZ"
    )
    assert "authkit.app" not in ISSUER
    assert authkit_jwks_url(CLIENT_ID) == (
        "https://api.workos.com/sso/jwks/client_01KZRZ1QQXA15KX04VQBZPE0DZ"
    )


def test_a_real_shaped_token_verifies_and_returns_the_workos_user_id():
    assert _verifier().verify(_token(sub="user_abc")) == "user_abc"


def test_a_token_with_no_aud_claim_is_accepted():
    # Explicit, because requiring `aud` is exactly what broke this before and
    # a future edit adding `audience=` back would pass every other test here.
    decoded = jwt.decode(_token(), options={"verify_signature": False})
    assert "aud" not in decoded
    assert _verifier().verify(_token()) == "user_01"


def test_a_token_for_another_workos_application_is_rejected():
    # Same provider, same signature, different customer. This is the check
    # that `aud` would normally do.
    with pytest.raises(InvalidCredentialsError):
        _verifier().verify(_token(client_id="client_someone_else"))


def test_a_token_with_no_client_id_claim_is_rejected():
    with pytest.raises(InvalidCredentialsError):
        _verifier().verify(_token(drop=("client_id",)))


def test_token_signed_by_an_unknown_key_is_rejected():
    with pytest.raises(InvalidCredentialsError):
        _verifier().verify(_token(key=_OTHER_KEY))


def test_wrong_issuer_is_rejected():
    with pytest.raises(InvalidCredentialsError):
        _verifier().verify(_token(iss="https://evil.example"))


def test_the_authkit_domain_as_issuer_is_rejected():
    # Guards the exact regression: someone "fixes" the issuer back to the
    # AuthKit domain because that is what the setting is named.
    with pytest.raises(InvalidCredentialsError):
        _verifier().verify(
            _token(iss="https://accurate-spoon-64-staging.authkit.app")
        )


def test_expired_token_is_rejected():
    with pytest.raises(InvalidCredentialsError):
        _verifier().verify(_token(exp_delta=-1))


def test_a_token_without_exp_is_rejected():
    # Without `require`, PyJWT happily accepts a token that never expires.
    with pytest.raises(InvalidCredentialsError):
        _verifier().verify(_token(drop=("exp",)))


def test_garbage_is_rejected_rather_than_raising_something_else():
    with pytest.raises(InvalidCredentialsError):
        _verifier().verify("not-a-jwt")


def test_token_without_sub_is_rejected():
    with pytest.raises(InvalidCredentialsError):
        _verifier().verify(_token(drop=("sub",)))


# --- the key resolver is I/O, so nothing unauthenticated may reach it ------
#
# In production the resolver fetches https://api.workos.com/sso/jwks/<client_id>
# whenever it meets a `kid` it has not seen. `current_user` is a sync def, so
# FastAPI runs it in the same AnyIO threadpool that X-API-Key order placement
# runs in: a stranger who can make an arbitrary request cause that fetch can
# hold the workers every trading bot needs. These tests pin the two guards.


def test_a_token_claiming_another_issuer_never_reaches_the_key_resolver():
    resolver = _CountingResolver()
    verifier = AuthKitVerifier(client_id=CLIENT_ID, key_resolver=resolver)

    with pytest.raises(InvalidCredentialsError):
        verifier.verify(_token(iss="https://evil.example"))

    assert resolver.calls == 0


def test_a_token_claiming_another_application_never_reaches_the_key_resolver():
    resolver = _CountingResolver()
    verifier = AuthKitVerifier(client_id=CLIENT_ID, key_resolver=resolver)

    with pytest.raises(InvalidCredentialsError):
        verifier.verify(_token(client_id="client_someone_else"))

    assert resolver.calls == 0


def test_a_well_formed_jwt_that_is_not_ours_never_reaches_the_key_resolver():
    # The cheap attack this guard exists for: any RS256 JWT at all, signed by
    # anyone, with a random `kid`. Before the guard it forced one live HTTPS
    # request to api.workos.com per request.
    resolver = _CountingResolver()
    verifier = AuthKitVerifier(client_id=CLIENT_ID, key_resolver=resolver)

    with pytest.raises(InvalidCredentialsError):
        verifier.verify(
            jwt.encode({"sub": "whoever"}, _OTHER_KEY, algorithm="RS256",
                       headers={"kid": "made-up"})
        )

    assert resolver.calls == 0


def test_garbage_never_reaches_the_key_resolver():
    resolver = _CountingResolver()
    verifier = AuthKitVerifier(client_id=CLIENT_ID, key_resolver=resolver)

    with pytest.raises(InvalidCredentialsError):
        verifier.verify("not-a-jwt")

    assert resolver.calls == 0


def test_a_token_that_claims_to_be_ours_still_reaches_the_key_resolver():
    # The other half: the gate reads UNSIGNED claims, so it may only filter.
    # A token claiming our issuer and application must still be verified for
    # real -- otherwise the guard above would have turned into the whole check.
    resolver = _CountingResolver()
    verifier = AuthKitVerifier(client_id=CLIENT_ID, key_resolver=resolver)

    assert verifier.verify(_token(sub="user_abc")) == "user_abc"
    assert resolver.calls == 1


def test_a_kid_already_resolved_is_answered_without_another_fetch():
    fetched: list[str] = []

    def fetch(token: str):
        fetched.append(token)
        return _KEY.public_key()

    resolve = cached_key_resolver(fetch)
    resolve(_token(kid="key_1", sub="user_a"))
    resolve(_token(kid="key_1", sub="user_b"))

    assert len(fetched) == 1


def test_an_unknown_kid_is_refused_while_a_fetch_is_already_in_flight():
    # PyJWKClient's unknown-`kid` refresh bypasses its own cache and does a
    # fresh HTTPS request with a 30s timeout, so without this the request path
    # would park one threadpool worker per unknown-kid token. Refusing beats
    # queueing: the caller retries, the bots keep their workers.
    in_fetch = threading.Event()
    finish = threading.Event()
    fetched: list[str] = []

    def fetch(token: str):
        fetched.append(token)
        in_fetch.set()
        finish.wait(5)
        return _KEY.public_key()

    resolve = cached_key_resolver(fetch)
    first = threading.Thread(target=resolve, args=(_token(kid="key_1"),))
    first.start()
    assert in_fetch.wait(5)

    with pytest.raises(InvalidCredentialsError):
        resolve(_token(kid="key_2"))
    assert len(fetched) == 1

    finish.set()
    first.join(5)

    # And the refusal is not sticky: once that fetch is done a new kid fetches
    # normally, so an upstream key rotation still costs one request, not an
    # outage.
    assert resolve(_token(kid="key_2")) is not None
    assert len(fetched) == 2


def test_the_remote_resolver_is_the_guarded_one(monkeypatch):
    # Both guards above are worthless if the production resolver is built
    # without them, and nothing else here can see that -- no test may build a
    # resolver that really fetches.
    fetched: list[str] = []

    class _FakeJwkClient:
        def __init__(self, url: str) -> None:
            self.url = url

        def get_signing_key_from_jwt(self, token: str):
            fetched.append(token)
            return SimpleNamespace(key=_KEY.public_key())

    monkeypatch.setattr(jwt, "PyJWKClient", _FakeJwkClient)
    resolve = remote_jwks_resolver(authkit_jwks_url(CLIENT_ID))

    resolve(_token(kid="key_1", sub="user_a"))
    resolve(_token(kid="key_1", sub="user_b"))

    assert len(fetched) == 1
