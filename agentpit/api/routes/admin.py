"""Admin-only routes. Guarded by an X-Admin-Token header matching
``Settings.admin_token``.

These endpoints exist for operations: flagging bot users out of public
leaderboards and creating competitions. They are not user-facing.
"""

import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field, model_validator

from agentpit.api.deps import CompetitionServiceDep, SessionDep, require_admin_token
from agentpit.api.routes.competitions import CompetitionWire
from agentpit.datastructures.competition import Competition, Prizes
from agentpit.db.table_write import TableWrite

router = APIRouter(
    tags=["admin"], prefix="/admin", dependencies=[Depends(require_admin_token)]
)


class MarkBotRequest(BaseModel):
    eth_address: str


class MarkBotResponse(BaseModel):
    eth_address: str
    is_bot: bool


Seconds = Annotated[int, Field(gt=0, lt=10**10)]


class CompetitionRequest(BaseModel):
    title: str = Field(min_length=1)
    startsAt: Seconds
    endsAt: Seconds
    prizeToken: str
    prizes: Prizes

    @model_validator(mode="after")
    def _ends_after_start(self) -> "CompetitionRequest":
        if self.endsAt <= self.startsAt:
            raise ValueError("endsAt must be after startsAt")
        return self


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


@router.put("/competitions/{slug}", response_model=CompetitionWire)
def put_competition(
    slug: Annotated[str, Path(pattern=r"^[a-z0-9-]{1,64}$")],
    payload: CompetitionRequest,
    service: CompetitionServiceDep,
) -> CompetitionWire:
    """Create or edit a competition. Its start and end change only before it starts."""
    competition = Competition(
        slug=slug,
        title=payload.title,
        starts_at=payload.startsAt,
        ends_at=payload.endsAt,
        prize_token=payload.prizeToken,
        prizes=payload.prizes,
    )
    return CompetitionWire.of(service.save(competition, int(time.time())))
