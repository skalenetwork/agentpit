import threading

import httpx
from fastapi import APIRouter, Depends, Response

from agentpit.api.deps import AgentServiceDep, LeaderboardServiceDep, SettingsDep, require_admin_token
from agentpit.api.og_card import fetch_robot, render_card
from agentpit.datastructures.create_agent_request import CreateAgentRequest
from agentpit.datastructures.create_agent_response import CreateAgentResponse

router = APIRouter(tags=["agents"])

# resvg holds the GIL for the whole ~200 ms render, and this process also
# serves every other route and runs the mirror feed. So a card is drawn once
# per change of its board row and then served from memory, and only one is
# drawn at a time. A request waits up to _RENDER_WAIT_S for its turn: link
# previews fetch a new card from several crawlers at once, and each fetches
# once, so the ones that wait must get the card the first one drew. Past
# that wait the answer is a 503 to retry. A row changes on every valuation
# pass, so an old card is never served again and the cap only bounds memory.
_CARDS_MAX = 64
_RENDER_WAIT_S = 1.0
_render_gate = threading.Lock()
# lowercase address -> (row key, png, robot svg)
_cards: dict[str, tuple[str, bytes, bytes]] = {}


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
    key = f"{row.model_dump_json()}|{ranked}|{valued_at}"
    lower = row.address.lower()
    cached = _cards.get(lower)
    if cached is None or cached[0] != key:
        # The robot only depends on the address: reuse it, and fetch a new
        # one outside the gate so a slow landing never holds up other draws.
        try:
            robot = cached[2] if cached is not None else fetch_robot(settings.landing_url, row.address)
        except httpx.HTTPError:
            return Response(status_code=503, headers={"Cache-Control": "no-store"})
        if not _render_gate.acquire(timeout=_RENDER_WAIT_S):
            return Response(status_code=503, headers={"Cache-Control": "no-store", "Retry-After": "1"})
        try:
            # Drawn by the request this one waited behind, most likely.
            cached = _cards.get(lower)
            if cached is None or cached[0] != key:
                cached = (key, render_card(row, ranked, valued_at, robot), robot)
                # Written under the gate, so only one thread ever changes the dict.
                _cards.pop(lower, None)
                if len(_cards) >= _CARDS_MAX:
                    del _cards[next(iter(_cards))]
                _cards[lower] = cached
        finally:
            _render_gate.release()
    return Response(cached[1], media_type="image/png", headers={"Cache-Control": "public, max-age=300"})
