import tomllib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    PlainSerializer,
    PlainValidator,
    StringConstraints,
    model_validator,
)

from agentpit.datastructures.market_context import MarketContext
from agentpit.domain.runner import Runner

LOCK = 86_400
PLACE_FLOOR = 1
MAX_POOL_USD = 5_000
HOUR = 3_600
POINT_LAG = 10
RECAP = 604_800
FINAL_DAY = 86_400
LEADERS = 6
LINES = 10
FIXTURES = 12
TAPE = 20

Phase = Literal["upcoming", "locked", "live", "ended"]
Raw = Annotated[int, PlainSerializer(str, return_type=str, when_used="json")]
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9-]{1,64}$")]
Title = Annotated[str, StringConstraints(max_length=64, pattern=r"^\S(.*\S)?$")]
Places = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,2}(-[1-9][0-9]{0,2})?$")]


def _seconds(value: object) -> int:
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0) or value.microsecond:
        raise ValueError("must be a UTC date and time in whole seconds, such as 2026-11-01T00:00:00Z")
    return int(value.timestamp())


Seconds = Annotated[int, PlainValidator(_seconds)]


class Prize(BaseModel):
    """What one place pays: whole USD paid in SKL at the end price, plus whole SKL."""

    model_config = ConfigDict(extra="forbid")

    usd: NonNegativeInt
    skl: NonNegativeInt

    @model_validator(mode="after")
    def _pays(self) -> "Prize":
        if not (self.usd or self.skl):
            raise ValueError("a place pays usd, skl or both")
        return self


class Competition(BaseModel):
    """A window over existing agent accounts with a prize per place; item `i`
    of `prizes` is place `i + 1`, and no prizes is practice."""

    slug: Slug
    title: str
    starts_at: int
    ends_at: int
    prizes: list[Prize]

    @property
    def locks_at(self) -> int:
        return self.starts_at - LOCK

    def phase(self, now: int) -> Phase:
        if now < self.locks_at:
            return "upcoming"
        if now < self.starts_at:
            return "locked"
        if now < self.ends_at:
            return "live"
        return "ended"


def _ladder(prizes: dict[str, Prize]) -> list[Prize]:
    """Each place's prize, place 1 first. Keys run from 1 with no gap or
    overlap, each pays less than the one above, and the pool stays within
    MAX_POOL_USD."""
    ladder: list[Prize] = []
    for places, prize in sorted(prizes.items(), key=lambda item: int(item[0].partition("-")[0])):
        first, _, last = places.partition("-")
        if int(first) != len(ladder) + 1 or (last and int(last) <= int(first)):
            raise ValueError(f"prizes {places}: places run from 1 with no gap or overlap, and a range ends above its start")
        if ladder and (prize.usd > ladder[-1].usd or prize.skl > ladder[-1].skl or prize == ladder[-1]):
            raise ValueError(f"prizes {places}: a place pays less than the place above")
        ladder += [prize] * (int(last or first) - int(first) + 1)
    if sum(prize.usd for prize in ladder) > MAX_POOL_USD:
        raise ValueError(f"prizes: the pool is over ${MAX_POOL_USD:,}")
    return ladder


class CompetitionFile(BaseModel):
    """competitions/<slug>.toml. No [prizes] table is practice."""

    model_config = ConfigDict(extra="forbid")

    title: Title
    starts_at: Seconds
    ends_at: Seconds
    prizes: Annotated[dict[Places, Prize], Field(min_length=1)] | None = None

    @model_validator(mode="after")
    def _checked(self) -> "CompetitionFile":
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        return self

    def competition(self, slug: str) -> Competition:
        return Competition(
            slug=slug,
            title=self.title,
            starts_at=self.starts_at,
            ends_at=self.ends_at,
            prizes=_ladder(self.prizes or {}),
        )


def load_competition(path: Path) -> Competition:
    """The competition a file defines; every fault is a ValueError."""
    return CompetitionFile.model_validate(tomllib.loads(path.read_text(encoding="utf-8")), strict=True).competition(path.stem)


def score(earned: int, base: int) -> int:
    """`earned` as a return on `base` in basis points, rounded half away from
    zero: the only rounding of a return in a race, whatever the scenario."""
    bp = (abs(earned) * 20_000 + base) // (2 * base)
    return bp if earned >= 0 else -bp


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class Score(_Strict):
    """A place and return in one scenario; no rank when it does not place there."""

    rank: int | None
    earned: Raw
    returnPct: float


class Standing(_Strict):
    """One entrant on the official basis, named by its handle or its short
    address, never its owner."""

    address: str
    name: str
    runner: Runner
    listed: bool
    runStartsAt: int
    endAt: int | None
    trades: int
    lastTradeAt: int | None
    disqualified: bool
    rank: int | None
    earned: Raw
    returnPct: float
    rankChange: int | None
    behind: Raw | None
    wouldRank: int | None
    hoursLed: int
    bids: Score | None
    bell: Score | None


class Line(_Strict):
    """An entrant's place at every column, then its place now."""

    address: str
    ranks: list[int | None]


class Series(_Strict):
    at: list[int]
    lines: list[Line]


class Stake(_Strict):
    """A position in a fixture, with its holder's place if outcome `i` wins."""

    address: str
    name: str
    outcome: int
    shares: float
    value: Raw
    places: list[int | None]


class Fixture(MarketContext):
    """A market still to settle before the end where an outcome moves a place."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    market: str
    question: str
    url: str | None
    outcomes: list[str]
    stakes: list[Stake]


class TapeFill(_Strict):
    """A run fill, or consecutive ones by one agent on one market, side and
    outcome within an hour, or a payout."""

    at: int
    address: str
    name: str
    kind: Literal["buy", "sell", "payout"]
    market: str
    question: str
    outcome: str | None
    price: float | None
    usd: Raw
    fills: int


class RaceHead(_Strict):
    slug: str
    title: str
    startsAt: int
    endsAt: int
    phase: Phase
    final: bool
    prizes: list[Prize]
    entrants: int
    ranked: int
    line: str


class RaceSummary(RaceHead):
    standings: list[Standing]


class Race(RaceHead):
    builtAt: int
    leadChanges: int
    standings: list[Standing]
    series: Series
    fixtures: list[Fixture]
    tape: list[TapeFill]


class RaceEntry(_Strict):
    race: RaceHead
    standing: Standing
    above: Standing | None
    below: Standing | None
    line: str


class Races(_Strict):
    competitions: list[RaceSummary]


class RaceEntries(_Strict):
    entries: list[RaceEntry]


class Capture(_Strict):
    raw: int
    at: int


class Holding(_Strict):
    """A token held at a start or end value: its shares and what they count
    for there, both in base units."""

    shares: int
    value: int


class Entry(_Strict):
    """An entry still in its race with what its standing is built from. The
    api key never leaves the service."""

    slug: str
    user_id: str
    api_key: str
    address: str
    handle: str | None
    app: str | None
    host: str | None
    run_starts_at: int
    start: Capture | None
    end: Capture | None
    end_block: int | None
    value_raw: int | None
    trades: int
    first_trade_at: int | None
    last_trade_at: int | None
    listed: bool
    disqualified: bool
