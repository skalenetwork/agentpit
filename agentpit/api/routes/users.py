import time

import psycopg.errors

from fastapi import APIRouter, Response
from pydantic import BaseModel
from web3 import Web3

from agentpit.api.deps import (
    AgentAccountsDep,
    AuthServiceDep,
    BalanceServiceDep,
    CurrentUserDep,
    LeaderboardServiceDep,
    OnchainAdminDep,
    OrderServiceDep,
    OwnerDep,
    SessionDep,
    SettingsDep,
)
from agentpit.datastructures.agent_summary import AgentSummary, NewAgent, OwnedAgent
from agentpit.datastructures.auth_response import UserPublic
from agentpit.datastructures.change_password_request import ChangePasswordRequest
from agentpit.datastructures.open_order import TitledOpenOrder
from agentpit.datastructures.private_key_request import (
    PrivateKeyRequest,
    PrivateKeyResponse,
)
from agentpit.datastructures.update_handle_request import UpdateHandleRequest
from agentpit.datastructures.user import User
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import HandleAlreadyExistsError, UserNotFoundError
from agentpit.services.leaderboard_service import RANK_FLOOR, rank_rows

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


@router.get("/me/agents", response_model=list[OwnedAgent])
def get_my_agents(
    user: CurrentUserDep, db: SessionDep, leaderboard: LeaderboardServiceDep, settings: SettingsDep
) -> list[OwnedAgent]:
    """Each agent with its board figures. One that is not on the board holds
    what it was handed and nothing else."""
    if user.workos_user_id is None:
        return []
    with db.read() as conn:
        agents = TableRead.agents_owned_by(conn, user.workos_user_id)
    if not agents:
        return []
    board = rank_rows(leaderboard.build_board(), "return")
    rows = {r.address: r for r in board}
    places = {r.address: i + 1 for i, r in enumerate(r for r in board if r.trades >= RANK_FLOOR)}
    owned: list[OwnedAgent] = []
    with db.read() as conn:
        for agent in agents:
            summary = AgentSummary.of(agent).model_dump()
            row = rows.get(agent.eth_address)
            if row is None:
                handed = TableRead.get_total_deposited(conn, agent.user_id, settings.paper_balance_target_raw)
                owned.append(OwnedAgent(**summary, equity=str(handed)))
                continue
            owned.append(
                OwnedAgent(
                    **summary,
                    equity=str(row.capital_raw),
                    trades=row.trades,
                    last_trade_at=row.last_trade_at,
                    earned=str(row.earned_raw),
                    return_pct=round(row.return_pct, 2),
                    place=places.get(row.address),
                )
            )
    return owned


@router.post("/me/agents", response_model=NewAgent, status_code=201)
def create_my_agent(owner: OwnerDep, accounts: AgentAccountsDep) -> NewAgent:
    agent = accounts.create_api_agent(owner)
    return NewAgent(**AgentSummary.of(agent).model_dump(), api_key=agent.api_key)


@router.patch("/me/agents/{address}", response_model=AgentSummary)
def rename_my_agent(
    address: str, payload: UpdateHandleRequest, owner: OwnerDep, db: SessionDep
) -> AgentSummary:
    agent = _owned_agent(db, owner, address)
    try:
        with db.write() as conn:
            TableWrite.update_user_handle(conn, agent.user_id, payload.handle)
    except psycopg.errors.UniqueViolation as exc:
        raise HandleAlreadyExistsError(payload.handle) from exc
    return AgentSummary.of(agent.model_copy(update={"handle": payload.handle}))


@router.delete("/me/agents/{address}", status_code=204)
def delete_my_agent(address: str, owner: OwnerDep, db: SessionDep, orders: OrderServiceDep) -> Response:
    agent = _owned_agent(db, owner, address)
    with db.write() as conn:
        TableWrite.delete_agent(conn, agent.user_id, int(time.time()))
    orders.cancel_all(agent)
    return Response(status_code=204)


@router.get("/me/agents/{address}/orders", response_model=list[TitledOpenOrder])
def list_my_agent_orders(
    address: str, owner: OwnerDep, db: SessionDep, orders: OrderServiceDep
) -> list[TitledOpenOrder]:
    open_orders = orders.list_open_orders(_owned_agent(db, owner, address))
    with db.read() as conn:
        titles = TableRead.questions_by_condition_id(conn, [o.market for o in open_orders])
    return [TitledOpenOrder(**o.model_dump(), title=titles.get(o.market, "")) for o in open_orders]


def _owned_agent(db: DbSession, owner: str, address: str) -> User:
    try:
        checksummed = Web3.to_checksum_address(address)
    except ValueError as exc:
        raise UserNotFoundError("agent not found") from exc
    with db.read() as conn:
        agent = TableRead.get_owned_agent(conn, owner, checksummed)
    if agent is None:
        raise UserNotFoundError("agent not found")
    return agent


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
