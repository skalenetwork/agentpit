from math import ceil
from typing import Literal

from pydantic import BaseModel, Field

from agentpit.datastructures.market_context import MarketContext

CLOSED_SHOWN = 100
PRICE_POINTS = 80

BetStatus = Literal["open", "won", "lost", "sold", "split"]


class BetFill(BaseModel):
    at: int
    side: Literal["BUY", "SELL", "REDEEM"]
    shares: float
    price: float | None
    dollars: str
    tx: str | None


class PriceLine(BaseModel):
    start: int
    minutes: list[int]
    permille: list[int]

    @classmethod
    def of(cls, points: list[tuple[int, int]]) -> "PriceLine | None":
        if not points:
            return None
        n = len(points)
        step = ceil((n - 1) / (PRICE_POINTS - 1)) if n > PRICE_POINTS else 1
        kept = [p for i, p in enumerate(points) if i % step == 0 or i == n - 1]
        start = kept[0][0]
        return cls(
            start=start,
            minutes=[(t - start + 30) // 60 for t, _ in kept],
            permille=[(price + 500) // 1000 for _, price in kept],
        )


class Bet(MarketContext):
    id: str
    conditionId: str = Field(exclude=True)
    title: str
    icon: str | None
    url: str | None
    category: str | None = None
    outcome: str
    status: BetStatus
    openedAt: int
    closedAt: int | None
    buys: int
    sells: int
    entry: float
    exit: float | None
    shares: float
    sold: float
    held: float
    cost: str
    back: str
    value: str
    sellsFor: str
    pnl: str
    fills: list[BetFill]
    prices: PriceLine | None
