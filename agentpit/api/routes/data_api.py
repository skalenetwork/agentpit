from typing import Annotated

from fastapi import APIRouter, Query

from agentpit.api.deps import AccountServiceDep
from agentpit.datastructures.activity_wire import ActivityWire
from agentpit.datastructures.position_wire import PositionWire

router = APIRouter(tags=["data-api"])

# As Polymarket's Data API pages /activity: a larger limit is clamped to
# MAX_ACTIVITY_LIMIT, an offset past MAX_ACTIVITY_OFFSET is refused.
MAX_ACTIVITY_LIMIT = 500
MAX_ACTIVITY_OFFSET = 5000


def _csv(value: str | None) -> list[str] | None:
    return [v for v in value.split(",") if v] if value else None


@router.get("/positions", response_model=list[PositionWire])
def get_positions(
    user: str, service: AccountServiceDep, market: str | None = None
) -> list[PositionWire]:
    if not service.is_public(user):
        return []
    return service.wallet(user, _csv(market)).positions


@router.get("/closed-positions", response_model=list[PositionWire])
def get_closed_positions(
    user: str, service: AccountServiceDep
) -> list[PositionWire]:
    """Resolved/cancelled positions (reconstructed from trade history), with
    realized payout + PnL — the Active /positions list drops them once redeemed."""
    if not service.is_public(user):
        return []
    return service.list_closed_positions(user)


@router.get("/value")
def get_value(user: str, service: AccountServiceDep) -> list[dict]:
    if not service.is_public(user):
        return [{"user": user, "value": 0}]
    return service.total_value(user)


@router.get("/activity", response_model=list[ActivityWire])
def get_activity(
    user: str,
    service: AccountServiceDep,
    type: str | None = None,
    market: str | None = None,
    limit: Annotated[int, Query(ge=0)] = 100,
    offset: Annotated[int, Query(ge=0, le=MAX_ACTIVITY_OFFSET)] = 0,
) -> list[ActivityWire]:
    if not service.is_public(user):
        return []
    return service.list_activity(
        user,
        type_filter=_csv(type),
        market=_csv(market),
        limit=min(limit, MAX_ACTIVITY_LIMIT),
        offset=offset,
    )
