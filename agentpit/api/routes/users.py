import time

import psycopg.errors

from fastapi import APIRouter, Response
from pydantic import BaseModel

from agentpit.api.deps import (
    AuthServiceDep,
    BalanceServiceDep,
    CurrentUserDep,
    OnchainAdminDep,
    SessionDep,
)
from agentpit.datastructures.agent_summary import AgentSummary
from agentpit.datastructures.auth_response import UserPublic
from agentpit.datastructures.change_password_request import ChangePasswordRequest
from agentpit.datastructures.private_key_request import (
    PrivateKeyRequest,
    PrivateKeyResponse,
)
from agentpit.datastructures.update_handle_request import UpdateHandleRequest
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import HandleAlreadyExistsError

router = APIRouter(tags=["users"])


class AutoRedeemRequest(BaseModel):
    enabled: bool


class TopUpStatusWire(BaseModel):
    nextAllowedAt: int


class TopUpWire(BaseModel):
    balance: str
    minted: str
    nextAllowedAt: int


class CreditsWire(BaseModel):
    credits_wei: str


@router.get("/me", response_model=UserPublic)
def get_me(user: CurrentUserDep) -> UserPublic:
    return UserPublic.model_validate(user.model_dump())


@router.get("/me/agents", response_model=list[AgentSummary])
def get_my_agents(user: CurrentUserDep, db: SessionDep) -> list[AgentSummary]:
    if user.workos_user_id is None:
        return []
    with db.read() as conn:
        return TableRead.agents_owned_by(conn, user.workos_user_id)


@router.patch("/me", response_model=UserPublic)
def update_me_handle(
    payload: UpdateHandleRequest,
    user: CurrentUserDep,
    db: SessionDep,
) -> UserPublic:
    try:
        with db.write() as conn:
            updated = TableWrite.update_user_handle(conn, user.user_id, payload.handle)
        if not updated:
            return UserPublic.model_validate(user.model_dump())
    except psycopg.errors.UniqueViolation as exc:
        raise HandleAlreadyExistsError(payload.handle) from exc

    with db.read() as conn:
        refreshed = TableRead.get_user_by_userid(conn, user.user_id)
    if refreshed is None:
        return UserPublic.model_validate(user.model_dump())
    return UserPublic.model_validate(refreshed.model_dump())


@router.patch("/me/password", response_model=UserPublic)
def update_me_password(
    payload: ChangePasswordRequest,
    user: CurrentUserDep,
    service: AuthServiceDep,
) -> UserPublic:
    service.change_password(
        user_id=user.user_id,
        current_password=payload.current_password,
        new_password=payload.new_password,
    )
    return UserPublic.model_validate(user.model_dump())


@router.patch("/me/auto-redeem", response_model=UserPublic)
def update_me_auto_redeem(
    payload: AutoRedeemRequest,
    user: CurrentUserDep,
    db: SessionDep,
) -> UserPublic:
    with db.write() as conn:
        TableWrite.set_auto_redeem(conn, user.user_id, payload.enabled)
        refreshed = TableRead.get_user_by_userid(conn, user.user_id)
    return UserPublic.model_validate((refreshed or user).model_dump())


@router.post("/me/private-key/code", status_code=202)
def send_private_key_code(user: CurrentUserDep, service: AuthServiceDep) -> dict:
    """Mail a fresh export code to this account's own address.

    The address comes off the authenticated row, never off the request.
    """
    service.send_key_export_code(user_id=user.user_id)
    return {"status": "sent"}


@router.post("/me/private-key", response_model=PrivateKeyResponse)
def export_me_private_key(
    payload: PrivateKeyRequest,
    user: CurrentUserDep,
    service: AuthServiceDep,
    response: Response,
) -> PrivateKeyResponse:
    """The account's own wallet key, to import into a wallet app.

    POST rather than GET on purpose: a key in a URL lands in proxy logs,
    browser history and the Referer header.
    """
    key = service.export_private_key(user_id=user.user_id, code=payload.code)
    response.headers["Cache-Control"] = "no-store"
    return PrivateKeyResponse(private_key=key, eth_address=user.eth_address)


@router.get("/me/top-up", response_model=TopUpStatusWire)
def get_top_up_status(
    user: CurrentUserDep, service: BalanceServiceDep
) -> TopUpStatusWire:
    """Cooldown status only — database read, no chain call, no mint, no write.

    The profile page already fetches the balance from `/balance-allowance`;
    re-reading it here would double an RPC round-trip on every page load for
    no benefit. This just tells the button when it may be clicked, so it can
    be disabled with a countdown instead of only failing after a click.
    """
    return TopUpStatusWire(nextAllowedAt=service.next_allowed(user))


@router.post("/me/top-up", response_model=TopUpWire)
def top_up_balance(user: CurrentUserDep, service: BalanceServiceDep) -> TopUpWire:
    """Restore the paper balance to the target, at most once a day.

    Returns 200 with `minted: "0"` when the cooldown is still running or the
    balance is already at the target — the button shows the reason, and neither
    case is an error worth an exception.
    """
    result = service.top_up(user, int(time.time()))
    return TopUpWire(
        balance=str(result.balance_raw),
        minted=str(result.minted_raw),
        nextAllowedAt=result.next_allowed_at,
    )


@router.get("/me/credits", response_model=CreditsWire)
def get_me_credits(user: CurrentUserDep, admin: OnchainAdminDep) -> CreditsWire:
    """The wallet's native balance -- what pays for a transaction.

    A string because wei overflows JavaScript's safe integer range, and the
    front end formats it rather than doing arithmetic on it.
    """
    return CreditsWire(credits_wei=str(admin.native_balance(user.eth_address)))
