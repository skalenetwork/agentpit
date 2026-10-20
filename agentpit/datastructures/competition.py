import tomllib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, NonNegativeInt, PlainValidator, StringConstraints, model_validator

LOCK = 86_400
PLACE_FLOOR = 1
MAX_POOL_USD = 5_000

Phase = Literal["upcoming", "locked", "live", "ended"]
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
