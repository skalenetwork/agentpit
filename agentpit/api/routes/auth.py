from fastapi import APIRouter, Request

from agentpit.api.deps import AuthKitServiceDep
from agentpit.datastructures.auth_response import AuthResponse, UserPublic
from agentpit.datastructures.authkit_requests import (
    CallbackRequest,
    CodeSignInRequest,
    RefreshRequest,
    SendCodeRequest,
)

router = APIRouter(tags=["auth"])


@router.post("/auth/code", status_code=202)
def send_auth_code(
    payload: SendCodeRequest, request: Request, service: AuthKitServiceDep
) -> dict:
    """Mail a six-digit code to this address.

    Always 202, whether or not the address has an account here: the reply must
    not tell a stranger who is registered. WorkOS creates the user on this call
    and mails the code; nothing is created on our side until the code comes
    back.
    """
    # `request.client.host` is the real caller only because uvicorn runs with
    # `--proxy-headers`, which rewrites it from `X-Forwarded-For` -- and only
    # from a proxy it is told to trust. Without that flag every caller would
    # look like Caddy and the per-IP rule would become a global kill switch;
    # trusting the header from anywhere would let a caller forge it and skip
    # the rule entirely. The precondition is that the api container publishes
    # no ports, so Caddy is the only thing that can reach it.
    client_ip = request.client.host if request.client else None
    service.send_code(payload.email, client_ip=client_ip)
    return {"status": "sent"}


@router.post("/auth/session", response_model=AuthResponse)
def sign_in_with_code(
    payload: CodeSignInRequest, service: AuthKitServiceDep
) -> AuthResponse:
    session = service.sign_in(payload.email, payload.code, payload.attribution)
    return AuthResponse(
        access_token=session.access_token,
        refresh_token=session.refresh_token,
        user=UserPublic.model_validate(session.user.model_dump()),
    )


@router.post("/auth/callback", response_model=AuthResponse)
def complete_callback(
    payload: CallbackRequest, service: AuthKitServiceDep
) -> AuthResponse:
    """Exchange the code a WorkOS redirect came back with.

    Provider-agnostic on purpose: `authorization_code` is not a Google grant,
    so this route takes a code from any provider WorkOS is configured with, and
    from the AuthKit Hosted UI, without a line of new code. Hence
    `/auth/callback` and not `/auth/google/callback`.

    The exchange is server-side because `client_secret` is our API key. That is
    why the redirect lands on a page and the code arrives here by POST: the
    browser may carry the code, never the secret.
    """
    session = service.sign_in_with_authorization_code(payload.code, payload.attribution)
    return AuthResponse(
        access_token=session.access_token,
        refresh_token=session.refresh_token,
        user=UserPublic.model_validate(session.user.model_dump()),
    )


@router.post("/auth/refresh", response_model=AuthResponse)
def refresh_session(
    payload: RefreshRequest, service: AuthKitServiceDep
) -> AuthResponse:
    session = service.refresh(payload.refresh_token)
    return AuthResponse(
        access_token=session.access_token,
        refresh_token=session.refresh_token,
        user=UserPublic.model_validate(session.user.model_dump()),
    )
