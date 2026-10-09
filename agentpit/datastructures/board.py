from typing import Literal

from pydantic import BaseModel

CardKind = Literal["binary", "multi", "matchup", "window"]
GameStatus = Literal["upcoming", "live", "started", "settled"]


class WireBet(BaseModel):
    agent: str
    name: str
    side: str
    value: str
    avgPrice: float
    pnl: str


class WireTeam(BaseModel):
    logo: str | None
    record: str | None
    color: str | None
    abbr: str | None


class WireOutcome(BaseModel):
    label: str
    question: str
    slug: str
    url: str | None
    market: int
    price: float | None
    ask: float | None
    change24h: float | None
    team: WireTeam | None
    bets: list[WireBet]


class WireCard(BaseModel):
    slug: str
    title: str
    icon: str | None
    category: str | None
    url: str | None
    volume: float | None
    kind: CardKind
    state: Literal["live", "settled"]
    startTime: int | None
    trading: bool | None
    endDate: int | None
    resolvedAt: int | None
    outcomeCount: int
    lead: int
    outcomes: list[WireOutcome]


class WireGame(WireCard):
    league: str
    leagueLabel: str
    sport: str
    status: GameStatus
    tz: str


class BoardTab(BaseModel):
    key: str
    label: str
    count: int
    category: bool


class SportItem(BaseModel):
    key: str
    label: str
    count: int


class Sport(SportItem):
    leagues: list[SportItem]


class Sports(BaseModel):
    item: str
    views: list[SportItem]
    sports: list[Sport]
    games: list[WireGame]
    futures: list[WireCard]
    settled: int
    noBook: dict[str, int]


class WireBoard(BaseModel):
    asOf: int
    liveMarkets: int
    tab: str
    tabs: list[BoardTab]
    q: str | None
    page: int
    pages: int
    total: int
    cards: list[WireCard]
    sports: Sports | None
