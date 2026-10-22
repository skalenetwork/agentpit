import httpx
from fastapi import APIRouter, Depends, Response

from agentpit.api.deps import AgentServiceDep, LeaderboardServiceDep, SettingsDep, require_admin_token
from agentpit.api.og_card import card, fetch_robot, render_card
from agentpit.datastructures.create_agent_request import CreateAgentRequest
from agentpit.datastructures.create_agent_response import CreateAgentResponse

router = APIRouter(tags=["agents"])


@router.post(
    "/create_agent",
    response_model=CreateAgentResponse,
    dependencies=[Depends(require_admin_token)],
)
def create_agent(
    payload: CreateAgentRequest, service: AgentServiceDep
) -> CreateAgentResponse:
    return service.create_agent(payload)


@router.get("/agents/{address}/card.png")
def get_agent_card(address: str, service: LeaderboardServiceDep, settings: SettingsDep) -> Response:
    """The share card the landing serves as the agent's og:image, drawn from its
    live board row. Public, any letter case; an agent not on the board is an
    empty 404."""
    board = service.build_board()
    row = next((r for r in board if r.address.lower() == address.lower()), None)
    if row is None:
        return Response(status_code=404)
    ranked = sum(r.place is not None for r in board)
    valued_at = service.holdings(row.address).valued_at
    try:
        png = card(
            row.address.lower(),
            f"{row.model_dump_json()}|{ranked}|{valued_at}",
            lambda robot: render_card(row, ranked, valued_at, robot),
            lambda: fetch_robot(settings.landing_url, row.address),
        )
    except httpx.HTTPError:
        return Response(status_code=503, headers={"Cache-Control": "no-store"})
    if png is None:
        return Response(status_code=503, headers={"Cache-Control": "no-store", "Retry-After": "1"})
    return Response(png, media_type="image/png", headers={"Cache-Control": "public, max-age=300"})
