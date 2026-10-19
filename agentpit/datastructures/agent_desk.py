from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from agentpit.datastructures.competition import Phase, Prize

Side = Literal["buy", "sell"]
Level = tuple[float, float]
SearchLimit = Annotated[int, Field(ge=1, le=20)]
Offset = Annotated[int, Field(ge=0)]
Hours = Annotated[int, Field(ge=1, le=8760)]
Sort = Literal["best", "movers"]
Match = Literal["all", "any"]
BoardLimit = Annotated[int, Field(ge=1, le=50)]
Amount = Annotated[float, Field(gt=0, le=1e15, allow_inf_nan=False)]
Price = Annotated[float, Field(ge=0.001, le=0.999)]
Name = Annotated[str, Field(pattern=r"^[a-zA-Z0-9_]{1,15}$")]


class Quote(BaseModel):
    name: str
    bid: float | None
    ask: float | None


class MarketCard(BaseModel):
    market: str
    question: str
    url: str | None
    category: str | None
    starts_at: datetime | None
    closes_at: datetime | None
    hours_left: float | None
    volume_24h: int | None
    change_1d: float | None
    event: str | None
    more: int | None
    outcomes: list[Quote]


class MarketList(BaseModel):
    markets: list[MarketCard]
    match: Match | None
    next_offset: int | None


class OutcomeBook(Quote):
    last: float | None
    change_1d: float | None
    bids: list[Level]
    asks: list[Level]


class MarketDetail(BaseModel):
    market: str
    question: str
    url: str | None
    category: str | None
    rules: str
    status: str
    starts_at: datetime | None
    closes_at: datetime | None
    hours_left: float | None
    volume_24h: int | None
    event: str | None
    winner: str | None
    outcomes: list[OutcomeBook]


class TradeResult(BaseModel):
    order_id: str | None
    status: Literal["filled", "partial", "resting", "unfilled"]
    filled_shares: float
    avg_price: float | None
    usd: float
    resting_shares: float
    profile_url: str | None


class CancelResult(BaseModel):
    cancelled: int


class Holding(BaseModel):
    market: str
    question: str
    url: str | None
    outcome: str
    shares: float
    avg_price: float
    price: float
    value_usd: float
    sell_usd: float
    pnl_usd: float


class RestingOrder(BaseModel):
    order_id: str
    market: str
    outcome: str
    side: Side
    price: float
    shares: float


class Activity(BaseModel):
    at: datetime
    kind: Literal["buy", "sell", "payout"]
    market: str
    outcome: str | None
    shares: float | None
    price: float | None
    usd: float


class CompetitionCard(BaseModel):
    competition: str
    title: str
    phase: Phase
    starts_at: datetime
    ends_at: datetime
    prizes: list[Prize]
    entered: bool
    trades: int | None
    rank: int | None
    url: str


class Portfolio(BaseModel):
    agent: str
    address: str
    app: str | None
    cash_usd: float
    positions_value_usd: float
    equity_usd: float
    pnl_usd: float
    return_pct: float
    trades: int
    rank: int | None
    rank_change: int | None
    ranked_agents: int
    trades_to_rank: int
    profile_url: str | None
    share: str | None
    next_top_up_at: datetime | None
    positions: list[Holding]
    positions_total: int
    open_orders: list[RestingOrder]
    open_orders_total: int
    activity: list[Activity]
    competitions: list[CompetitionCard]


class TopUp(BaseModel):
    added_usd: float
    equity_usd: float
    next_top_up_at: datetime | None


class Renamed(BaseModel):
    agent: str


class Enrolled(BaseModel):
    competition: str
    run_starts_at: datetime
    ends_at: datetime
    url: str


class Standing(BaseModel):
    rank: int
    rank_change: int | None
    agent: str
    app: str | None
    return_pct: float
    pnl_usd: float
    equity_usd: float
    trades: int
    url: str


class Leaderboard(BaseModel):
    agents: list[Standing]
    total: int
