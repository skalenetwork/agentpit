from fastapi import APIRouter, Depends, HTTPException, Query, Response

from agentpit.api.deps import LeaderboardServiceDep, MarketServiceDep, SessionDep, SettingsDep, require_admin_token
from agentpit.datastructures.board import WireBoard
from agentpit.datastructures.cancel_market_response import CancelMarketResponse
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.gamma_market import GammaMarket
from agentpit.datastructures.list_markets_response import MarketStatsResponse
from agentpit.datastructures.market import Market
from agentpit.datastructures.resolve_market_request import ResolveMarketRequest
from agentpit.services.board_service import BoardService

router = APIRouter(tags=["markets"])


def _csv(value: str | None) -> list[str] | None:
    return [v for v in value.split(",") if v] if value else None


@router.get("/markets", response_model=list[GammaMarket])
def list_markets(
    service: MarketServiceDep,
    limit: int = 100,
    offset: int = 0,
    id: int | None = None,
    slug: str | None = None,
    condition_ids: str | None = None,
    clob_token_ids: str | None = None,
    polymarket_condition_id: str | None = None,
) -> list[GammaMarket]:
    return service.list_markets_gamma(
        limit=limit,
        offset=offset,
        market_id=id,
        slug=slug,
        condition_ids=_csv(condition_ids),
        clob_token_ids=_csv(clob_token_ids),
        polymarket_condition_id=polymarket_condition_id,
    )


@router.post(
    "/markets",
    response_model=Market,
    dependencies=[Depends(require_admin_token)],
)
def create_market(payload: CreateMarketRequest, service: MarketServiceDep) -> Market:
    if payload.condition_id is not None:
        raise HTTPException(
            status_code=400, detail="condition_id is derived on chain from question_id; omit it"
        )
    return service.create_market(payload)


# NOTE: declaration order is load-bearing, as for /events/categories. FastAPI
# matches in registration order, so this MUST stay above /markets/{market_id} —
# below it, "stats" is matched as a market_id and rejected as a non-integer (422).
@router.get("/markets/stats", response_model=MarketStatsResponse)
def market_stats(service: MarketServiceDep) -> MarketStatsResponse:
    return service.market_stats()


@router.get("/markets/board", response_model=WireBoard)
def market_board(
    response: Response,
    db: SessionDep,
    leaderboard: LeaderboardServiceDep,
    settings: SettingsDep,
    tab: str = "trending",
    q: str | None = None,
    page: int = Query(default=1, ge=1),
    sport: str = "upcoming",
) -> WireBoard:
    """The public /markets page in one read: tab counts, one page of event cards, or the Sports section."""
    found = BoardService(db, leaderboard, settings).board(tab, (q or "").strip() or None, page, sport)
    if found is None:
        raise HTTPException(status_code=404, detail="no such tab")
    response.headers["Cache-Control"] = "public, max-age=30"
    return found


@router.get("/markets/{market_id}", response_model=GammaMarket)
def get_market(market_id: int, service: MarketServiceDep) -> GammaMarket:
    return service.get_market_gamma(market_id)


@router.post(
    "/markets/{market_id}/activate",
    response_model=Market,
    dependencies=[Depends(require_admin_token)],
)
def activate_market(market_id: int, service: MarketServiceDep) -> Market:
    return service.activate_market(market_id)


@router.post(
    "/markets/{market_id}/close",
    response_model=Market,
    dependencies=[Depends(require_admin_token)],
)
def close_market(market_id: int, service: MarketServiceDep) -> Market:
    return service.close_market(market_id)


@router.post(
    "/markets/{market_id}/cancel",
    response_model=CancelMarketResponse,
    dependencies=[Depends(require_admin_token)],
)
def cancel_market(market_id: int, service: MarketServiceDep) -> CancelMarketResponse:
    return service.cancel_market(market_id)


@router.post(
    "/markets/{market_id}/resolve",
    response_model=Market,
    dependencies=[Depends(require_admin_token)],
)
def resolve_market(
    market_id: int,
    payload: ResolveMarketRequest,
    service: MarketServiceDep,
) -> Market:
    return service.resolve_market(market_id, payload)
