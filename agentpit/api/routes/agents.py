import httpx
from fastapi import APIRouter, Depends, Response
from web3 import Web3

from agentpit.api.deps import AgentServiceDep, SessionDep, SettingsDep, require_admin_token
from agentpit.api.og_card import fetch_robot, render_card
from agentpit.datastructures.create_agent_request import CreateAgentRequest
from agentpit.datastructures.create_agent_response import CreateAgentResponse
from agentpit.db.table_read import TableRead
from agentpit.domain.runner import runner_for
from agentpit.services.leaderboard_service import display_name

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
def get_agent_card(address: str, db: SessionDep, settings: SettingsDep) -> Response:
    """The share card the landing serves as the agent's og:image. Public, any letter case."""
    try:
        checksummed = Web3.to_checksum_address(address)
    except ValueError:
        return Response(status_code=404)
    with db.read() as conn:
        user = TableRead.get_user_by_eth_address(conn, checksummed)
    if user is None:
        return Response(status_code=404)
    try:
        robot = fetch_robot(settings.landing_url, user.eth_address)
    except httpx.HTTPError:
        return Response(status_code=503, headers={"Cache-Control": "no-store"})
    png = render_card(
        display_name(user.handle, user.eth_address),
        user.eth_address,
        runner_for(user.agent_app, user.agent_host),
        robot,
    )
    return Response(png, media_type="image/png", headers={"Cache-Control": "public, max-age=604800"})
