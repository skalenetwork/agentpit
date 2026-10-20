"""Prize competitions and their standings, public."""
import time

from fastapi import APIRouter
from pydantic import BaseModel

from agentpit.api.deps import CompetitionServiceDep
from agentpit.datastructures.competition import Phase, Prize
from agentpit.domain.runner import Runner
from agentpit.services.competition_service import Page, Standing

router = APIRouter(tags=["competitions"])

LIST_STANDINGS = 6


class StandingWire(BaseModel):
    rank: int | None
    name: str
    address: str
    runner: Runner
    listed: bool
    runStartsAt: int
    earned: str
    returnPct: float
    trades: int
    disqualified: bool

    @classmethod
    def of(cls, standing: Standing) -> "StandingWire":
        return cls(
            rank=standing.rank,
            name=standing.name,
            address=standing.address,
            runner=standing.runner,
            listed=standing.listed,
            runStartsAt=standing.run_starts_at,
            earned=str(standing.earned_raw),
            returnPct=standing.return_pct,
            trades=standing.trades,
            disqualified=standing.disqualified,
        )


class CompetitionWire(BaseModel):
    slug: str
    title: str
    startsAt: int
    locksAt: int
    endsAt: int
    phase: Phase
    prizes: list[Prize]
    entrants: int
    final: bool
    standings: list[StandingWire]

    @classmethod
    def of(cls, page: Page, shown: int | None = None) -> "CompetitionWire":
        competition = page.competition
        return cls(
            slug=competition.slug,
            title=competition.title,
            startsAt=competition.starts_at,
            locksAt=competition.locks_at,
            endsAt=competition.ends_at,
            phase=page.phase,
            prizes=competition.prizes,
            entrants=page.entrants,
            final=page.final,
            standings=[StandingWire.of(standing) for standing in page.standings[:shown]],
        )


class CompetitionsWire(BaseModel):
    competitions: list[CompetitionWire]


@router.get("/competitions", response_model=CompetitionsWire)
def list_competitions(service: CompetitionServiceDep) -> CompetitionsWire:
    """Every competition, latest start first, with the first 6 standings of
    its page. A prize is whole USD paid in SKL plus whole SKL; place `i + 1`
    is `prizes[i]`, and no prizes is practice."""
    now = int(time.time())
    return CompetitionsWire(
        competitions=[CompetitionWire.of(service.page(c.competition.slug, now), LIST_STANDINGS) for c in service.listing(now)]
    )


@router.get("/competitions/{slug}", response_model=CompetitionWire)
def get_competition(slug: str, service: CompetitionServiceDep) -> CompetitionWire:
    """One competition with its standings, 404 when there is none. `earned`
    is a base-unit integer string; `final` once every end value is taken.
    Owners never appear."""
    return CompetitionWire.of(service.page(slug, int(time.time())))
