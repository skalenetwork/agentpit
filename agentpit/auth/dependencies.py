from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import (
    APIKeyHeader,
    HTTPAuthorizationCredentials,
    HTTPBearer,
)

from agentpit.auth.authkit_tokens import AuthKitVerifier
from agentpit.datastructures.user import User
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.domain.exceptions import InvalidCredentialsError

_bearer = HTTPBearer(auto_error=False)
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _unauth(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _authkit_user(db: DbSession, authkit: AuthKitVerifier, token: str) -> User:
    """The account an AuthKit access token belongs to -- never a new one.

    A token that verifies but matches no row is rejected, not onboarded.
    Creating the account here would hand a funded wallet to any valid AuthKit
    session for our application that touched any authenticated route; creation
    belongs to `POST /auth/session` alone, which has the mailed code as proof
    the caller owns the address.
    """
    try:
        workos_user_id = authkit.verify(token)
    except InvalidCredentialsError:
        # Deliberately the same wording the unconfigured-deployment branch in
        # `current_user` uses: why a bearer token was refused is nothing a
        # caller should be able to probe for.
        raise _unauth("invalid token")

    with db.read() as conn:
        user = TableRead.get_user_by_workos_id(conn, workos_user_id)
    if user is None:
        raise _unauth("unknown session")
    return user


def make_current_user_dep(authkit: AuthKitVerifier | None):
    """Build a FastAPI dependency that resolves a request credential to a User.

    Two accepted credentials since the cutover: a long-lived `X-API-Key`
    header, checked first, and an AuthKit access token as a bearer. The
    verifier is captured by closure so tests can swap it via
    dependency_overrides.

    `authkit` is None on a deployment with no `WORKOS_CLIENT_ID` -- the issuer
    and the JWKS URL are both derived from it, so a verifier built without one
    could only reject everything. Such a deployment can still authenticate
    bots by `X-API-Key`, and nobody else.
    """
    from agentpit.api.deps import get_db_session

    def current_user(
        api_key: Annotated[str | None, Depends(_api_key_header)],
        creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
        db: Annotated[DbSession, Depends(get_db_session)],
    ) -> User:
        if api_key:
            with db.read() as conn:
                user = TableRead.get_user_by_api_key(conn, api_key)
            if user is None or user.deleted_at is not None:
                raise _unauth("invalid api key")
            return user

        if creds is None or not creds.credentials:
            raise _unauth("missing credentials")

        # EVERY bearer token now reaches the verifier -- a legacy `JwtCoder`
        # one, an expired AuthKit one, outright junk. The local HMAC check that
        # used to absorb the first two and answer without I/O is gone, so the
        # gate at the top of `AuthKitVerifier.verify` and the single-flight
        # guard in `cached_key_resolver` are the only things left between an
        # unauthenticated caller and one live fetch to api.workos.com per
        # request, on the threadpool the X-API-Key path shares. They matter
        # more after this change than they did before it.
        if authkit is None:
            raise _unauth("invalid token")
        return _authkit_user(db, authkit, creds.credentials)

    return current_user
