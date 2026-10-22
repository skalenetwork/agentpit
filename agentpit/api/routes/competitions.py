"""Prize competitions and their races, public."""
import time

import httpx
from fastapi import APIRouter, Response

from agentpit.api.deps import CompetitionServiceDep, SettingsDep
from agentpit.api.og_card import race_card
from agentpit.datastructures.competition import HOUR, RECAP, Line, Race, RaceEntries, Races
from agentpit.domain.exceptions import NotFoundError

router = APIRouter(tags=["competitions"])


@router.get("/competitions", response_model=Races)
def list_competitions(service: CompetitionServiceDep) -> Races:
    """Every competition, latest start first, with its first standings. A
    prize is whole USD paid in SKL plus whole SKL; place `i + 1` is
    `prizes[i]`, and no prizes is practice."""
    return Races(competitions=[build.summary for build in service.races(int(time.time())).values()])


@router.get("/competitions/{slug}", response_model=Race)
def get_competition(slug: str, service: CompetitionServiceDep, agent: str | None = None) -> Race:
    """One race, 404 when there is none. With `agent`, the address of an
    entrant in any letter case, `line` is that agent's sentence and the chart
    carries its line; any other agent is ignored. Money is a base-unit
    integer string; owners never appear."""
    build = service.races(int(time.time())).get(slug)
    if build is None:
        raise NotFoundError(f"There is no competition called {slug}.")
    entry = build.entries.get((agent or "").lower())
    if entry is None:
        return build.race
    race, address = build.race, entry.standing.address
    lines = race.series.lines
    if all(line.address != address for line in lines):
        lines = [*lines, Line(address=address, ranks=[*build.ranks.get(address, [None] * len(race.series.at)), entry.standing.rank])]
    return race.model_copy(update={"line": entry.line, "series": race.series.model_copy(update={"lines": lines})})


@router.get("/competitions/{slug}/card.png")
def get_competition_card(slug: str, service: CompetitionServiceDep, settings: SettingsDep, final: bool = False) -> Response:
    """The race's share card, drawn once per UTC hour and once for good when
    final. An empty 404 when there is no such race, and with `final` until it
    is final."""
    now = int(time.time())
    build = service.races(now).get(slug)
    if build is None or final and not build.race.final:
        return Response(status_code=404)
    try:
        png = race_card(build.race, now - now % HOUR, settings.landing_url)
    except httpx.HTTPError:
        return Response(status_code=503, headers={"Cache-Control": "no-store"})
    if png is None:
        return Response(status_code=503, headers={"Cache-Control": "no-store", "Retry-After": "1"})
    return Response(png, media_type="image/png", headers={"Cache-Control": f"public, max-age={2_592_000 if build.race.final else 300}"})


@router.get("/agents/{address}/competitions", response_model=RaceEntries)
def get_agent_competitions(address: str, service: CompetitionServiceDep) -> RaceEntries:
    """The agent's entry in every race it is entered in, kept RECAP past the
    end, latest start first. Public, any letter case."""
    now = int(time.time())
    return RaceEntries(
        entries=[
            entry
            for build in service.races(now).values()
            if build.race.endsAt > now - RECAP and (entry := build.entries.get(address.lower())) is not None
        ]
    )
