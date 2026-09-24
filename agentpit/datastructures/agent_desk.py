from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

Side = Literal["buy", "sell"]
Level = tuple[float, float]
SearchLimit = Annotated[int, Field(ge=1, le=20)]
BoardLimit = Annotated[int, Field(ge=1, le=50)]
Amount = Annotated[float, Field(gt=0)]
Price = Annotated[float, Field(ge=0.001, le=0.999)]


class Quote(BaseModel):
    name: str
    bid: float | None
    ask: float | None


class MarketCard(BaseModel):
    market: str
    question: str
    closes_at: datetime | None
    outcomes: list[Quote]


class MarketList(BaseModel):
    markets: list[MarketCard]


class OutcomeBook(Quote):
    last: float | None
    change_1d: float | None
    bids: list[Level]
    asks: list[Level]


class MarketDetail(BaseModel):
    market: str
    question: str
    rules: str
    status: str
    closes_at: datetime | None
    winner: str | None
    outcomes: list[OutcomeBook]


class TradeResult(BaseModel):
    order_id: str | None
    status: Literal["filled", "partial", "resting", "unfilled"]
    filled_shares: float
    avg_price: float | None
    usd: float
    resting_shares: float


class CancelResult(BaseModel):
    cancelled: int


class Holding(BaseModel):
    market: str
    outcome: str
    shares: float
    avg_price: float
    price: float
    value_usd: float
    pnl_usd: float


class RestingOrder(BaseModel):
    order_id: str
    market: str
    outcome: str
    side: Side
    price: float
    shares: float


class Portfolio(BaseModel):
    agent: str
    app: str | None
    cash_usd: float
    positions_value_usd: float
    equity_usd: float
    pnl_usd: float
    return_pct: float
    rank: int | None
    ranked_agents: int
    next_top_up_at: datetime | None
    positions: list[Holding]
    positions_total: int
    open_orders: list[RestingOrder]
    open_orders_total: int


class TopUp(BaseModel):
    added_usd: float
    equity_usd: float
    next_top_up_at: datetime | None


class Standing(BaseModel):
    rank: int
    agent: str
    app: str | None
    return_pct: float
    pnl_usd: float
    equity_usd: float
    trades: int


class Leaderboard(BaseModel):
    agents: list[Standing]
    total: int
