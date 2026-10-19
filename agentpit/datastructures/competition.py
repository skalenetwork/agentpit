from typing import Annotated, Literal

from pydantic import BaseModel, Field, NonNegativeInt

LOCK = 86_400

Phase = Literal["upcoming", "locked", "live", "ended"]


class Prize(BaseModel):
    """One place: a USD amount paid in the prize token plus a fixed token amount."""

    usd: NonNegativeInt = 0
    tokens: NonNegativeInt = 0


Prizes = Annotated[list[Prize], Field(min_length=1)]


class Competition(BaseModel):
    """A window over existing agent accounts with a prize per place; item `i`
    of `prizes` is place `i + 1`."""

    slug: str
    title: str
    starts_at: int
    ends_at: int
    prize_token: str
    prizes: Prizes

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
