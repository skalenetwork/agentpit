"""Prize competitions and their standings, public."""
import time

from fastapi import APIRouter
from pydantic import BaseModel

from agentpit.api.deps import CompetitionServiceDep
from agentpit.datastructures.competition import Phase, Prize
from agentpit.domain.runner import Runner
from agentpit.services.competition_service import Listing, Standing

router = APIRouter(tags=["competitions"])


class CompetitionWire(BaseModel):
    slug: str
    title: str
    startsAt: int
    locksAt: int
    endsAt: int
    phase: Phase
    prizeToken: str
    prizes: list[Prize]
    entrants: int

    @classmethod
    def of(cls, listing: Listing) -> "CompetitionWire":
        competition = listing.competition
        return cls(
            slug=competition.slug,
            title=competition.title,
            startsAt=competition.starts_at,
            locksAt=competition.locks_at,
            endsAt=competition.ends_at,
            phase=listing.phase,
            prizeToken=competition.prize_token,
            prizes=competition.prizes,
            entrants=listing.entrants,
        )


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


class CompetitionPageWire(CompetitionWire):
    final: bool
    standings: list[StandingWire]


class CompetitionsWire(BaseModel):
    competitions: list[CompetitionWire]


@router.get("/competitions", response_model=CompetitionsWire)
def list_competitions(service: CompetitionServiceDep) -> CompetitionsWire:
    """Every competition, latest start first. A prize is whole USD paid in
    `prizeToken` plus whole tokens; place `i + 1` is `prizes[i]`."""
    return CompetitionsWire(
        competitions=[CompetitionWire.of(listing) for listing in service.listing(int(time.time()))]
    )


@router.get("/competitions/{slug}", response_model=CompetitionPageWire)
def get_competition(slug: str, service: CompetitionServiceDep) -> CompetitionPageWire:
    """One competition with its standings, 404 when there is none. `earned`
    is a base-unit integer string; `final` once every end value is taken.
    Owners never appear."""
    page = service.page(slug, int(time.time()))
    return CompetitionPageWire(
        **CompetitionWire.of(page).model_dump(),
        final=page.final,
        standings=[StandingWire.of(standing) for standing in page.standings],
    )
