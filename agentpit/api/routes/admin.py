"""Admin-only routes. Guarded by an X-Admin-Token header matching
``Settings.admin_token``.

These endpoints exist for operational bot management (flagging bot users
out of public leaderboards). They are not user-facing.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from agentpit.api.deps import SessionDep, require_admin_token
from agentpit.db.table_write import TableWrite

router = APIRouter(
    tags=["admin"], prefix="/admin", dependencies=[Depends(require_admin_token)]
)


class MarkBotRequest(BaseModel):
    eth_address: str


class MarkBotResponse(BaseModel):
    eth_address: str
    is_bot: bool


@router.post("/mark_bot", response_model=MarkBotResponse)
def mark_bot(payload: MarkBotRequest, db: SessionDep) -> MarkBotResponse:
    with db.write() as conn:
        updated = TableWrite.mark_user_as_bot_by_eth_address(conn, payload.eth_address)
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no user with eth_address {payload.eth_address}",
        )
    return MarkBotResponse(eth_address=payload.eth_address, is_bot=True)
