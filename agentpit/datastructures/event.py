from typing import Optional
from pydantic import BaseModel

from agentpit.common import check_state


class Event(BaseModel):
    event_id: int
    slug: str
    title: str
    description: str = ""
    icon_url: Optional[str] = None
    category: Optional[str] = None
    start_date: Optional[int] = None
    end_date: Optional[int] = None
    polymarket_event_id: Optional[str] = None
    # Upstream Polymarket 24h volume (event- or series-level), captured at sync
    # time. Drives the homepage ordering. None when never synced from upstream.
    volume_24hr: Optional[float] = None
    # Upstream all-time volume, captured at the same time. This is the figure
    # the cards display; volume_24hr stays the ranking key.
    volume: Optional[float] = None
    # Order-book depth in dollars, captured at sync time. Drives the Liquidity
    # sort. None when never synced from upstream.
    liquidity: Optional[float] = None
    # How contested the odds are, 0..1 — a 50/50 market scores near 1. Captured
    # at the same time; independent of liquidity. None when never synced.
    competitive: Optional[float] = None
    start_time: int | None = None
    game_id: str | None = None
    series_slug: str | None = None

    @property
    def url(self) -> str | None:
        if self.polymarket_event_id is None:
            return None
        return f"https://polymarket.com/event/{self.slug}"

    def model_post_init(self, _context):
        check_state(len(self.title) > 0, "Event title must not be empty")
        check_state(len(self.slug) > 0, "Event slug must not be empty")
