"""One agent's public page: its board row, the latest valuation's holdings and its newest fills."""
from datetime import date
from itertools import accumulate
from typing import Literal

from pydantic import BaseModel

from agentpit.datastructures.activity_wire import ActivityWire
from agentpit.datastructures.market_context import MarketContext
from agentpit.domain.runner import Runner, runner_for
from agentpit.services.leaderboard_service import Holdings, LeaderboardRow, share_text

ACTIVITY = 40
PNLS = 40

Tag = Literal["busiest", "closestBattle"]


class Neighbour(BaseModel):
    place: int | None
    name: str
    address: str
    returnPct: float
    trades: int


class Standing(BaseModel):
    place: int | None
    placeChange: int | None
    rankedCount: int
    warmingCount: int
    gap: float | None
    tags: list[Tag]
    neighbours: list[Neighbour]


class Money(BaseModel):
    returnPct: float
    capital: str
    deposited: str
    earned: str
    cash: str
    invested: str
    unrealized: str
    trendStart: date | None
    trend: list[str]


class OpenPosition(MarketContext):
    title: str
    icon: str | None
    category: str | None
    outcome: str
    avgPrice: float
    curPrice: float
    value: str
    sellsFor: str
    pnl: str
    url: str | None


class Positions(BaseModel):
    count: int
    mark: str
    sellsFor: str
    top: list[OpenPosition]
    open: list[OpenPosition]


class CategoryShare(BaseModel):
    category: str | None
    share: float
    count: int


class Bin(BaseModel):
    yes: str
    no: str


class Book(BaseModel):
    mix: list[CategoryShare]
    entryPrice: list[Bin]
    horizonDays: int | None


class Call(MarketContext):
    title: str
    icon: str | None
    category: str | None
    outcome: str
    entry: float
    exit: float
    pnl: str
    url: str | None


class Record(BaseModel):
    wins: int
    losses: int
    pnls: list[str]
    best: Call
    worst: Call


class Fill(MarketContext):
    at: int
    type: Literal["TRADE", "SPLIT", "MERGE", "REDEEM"]
    side: Literal["BUY", "SELL"] | None
    outcome: str | None
    shares: float
    dollars: str
    title: str
    icon: str | None
    category: str | None
    url: str | None


class AgentProfile(BaseModel):
    name: str
    address: str
    runner: Runner
    trades: int
    firstTradeAt: int
    lastTradeAt: int
    valuedAt: int
    standing: Standing
    share: str
    money: Money
    positions: Positions
    book: Book | None
    record: Record | None
    activity: list[Fill]


def _money(dollars: float) -> str:
    return str(round(dollars * 1_000_000))


def build_profile(
    row: LeaderboardRow,
    board: list[LeaderboardRow],
    held: Holdings,
    fills: list[ActivityWire],
    categories: dict[str, str],
    contexts: dict[str, MarketContext],
    now: int,
) -> AgentProfile:
    """`board` in return order. Returns compare as the board shows them, rounded
    to 2 places, so every tie resolves the way the landing resolves it."""
    shown = {r.address: round(r.return_pct, 2) for r in board}
    ranked = [r for r in board if r.place is not None]
    warming = sorted((r for r in board if r.place is None), key=lambda r: r.trades, reverse=True)
    own = ranked if row.place else warming
    i = own.index(row)
    start = min(max(0, i - 2), max(0, len(own) - 5))
    gap = None
    if row.place and len(ranked) > 1:
        gap = round(abs(shown[row.address] - shown[ranked[i - 1 if i else 1].address]), 2)

    tags: list[Tag] = []
    if ranked and max(ranked, key=lambda r: r.trades) is row:
        tags.append("busiest")
    pairs = list(zip(ranked, ranked[1:]))
    if pairs and row in min(pairs, key=lambda p: abs(shown[p[0].address] - shown[p[1].address])):
        tags.append("closestBattle")

    open_ = [p for p in held.positions if not p.settled]
    settled = [p for p in held.positions if p.settled]
    listed = [
        OpenPosition(
            **contexts.get(p.conditionId, MarketContext()).model_dump(),
            title=p.title,
            icon=p.icon or None,
            category=categories.get(p.conditionId),
            outcome=p.outcome,
            avgPrice=p.avgPrice,
            curPrice=p.curPrice,
            value=_money(p.currentValue),
            sellsFor=_money(p.sellableValue),
            pnl=_money(p.cashPnl),
            url=p.url,
        )
        for p in sorted(open_, key=lambda p: p.currentValue, reverse=True)
    ]
    book = None
    if len(open_) >= 3:
        cost = sum(p.initialValue for p in open_)
        groups: dict[str | None, list[float]] = {}
        bins = [{"Yes": 0.0, "No": 0.0} for _ in range(10)]
        for p in open_:
            groups.setdefault(categories.get(p.conditionId), []).append(p.initialValue)
            if p.avgPrice > 0 and p.outcome in ("Yes", "No"):
                bins[min(9, int(p.avgPrice * 10))][p.outcome] += p.initialValue
        ends = sorted((int(p.endDate), p.initialValue) for p in open_ if p.endDate and p.initialValue > 0)
        half = sum(c for _, c in ends) / 2
        book = Book(
            mix=sorted(
                (
                    CategoryShare(category=c, share=sum(costs) / cost if cost else 0.0, count=len(costs))
                    for c, costs in groups.items()
                ),
                key=lambda s: (s.share, s.count),
                reverse=True,
            ),
            entryPrice=[Bin(yes=_money(b["Yes"]), no=_money(b["No"])) for b in bins],
            horizonDays=next(
                (
                    max(0, round((end - now) / 86_400))
                    for (end, _), running in zip(ends, accumulate(c for _, c in ends))
                    if running >= half
                ),
                None,
            ),
        )

    decided = sorted(
        (p for p in held.closed + settled if p.avgPrice > 0 and abs(p.cashPnl) > 0.5),
        key=lambda p: int(p.endDate or 0),
    )
    record = None
    if decided:
        best, worst = (
            Call(
                **contexts.get(p.conditionId, MarketContext()).model_dump(),
                title=p.title,
                icon=p.icon or None,
                category=categories.get(p.conditionId),
                outcome=p.outcome,
                entry=p.avgPrice,
                exit=p.curPrice,
                pnl=_money(p.cashPnl),
                url=p.url,
            )
            for p in (max(decided, key=lambda p: p.cashPnl), min(decided, key=lambda p: p.cashPnl))
        )
        record = Record(
            wins=sum(p.cashPnl > 0 for p in decided),
            losses=sum(p.cashPnl < 0 for p in decided),
            pnls=[_money(p.cashPnl) for p in decided[-PNLS:]],
            best=best,
            worst=worst,
        )

    return AgentProfile(
        name=row.name,
        address=row.address,
        runner=runner_for(row.app, row.host),
        trades=row.trades,
        firstTradeAt=row.first_trade_at,
        lastTradeAt=row.last_trade_at,
        valuedAt=held.valued_at,
        standing=Standing(
            place=row.place,
            placeChange=row.place_change,
            rankedCount=len(ranked),
            warmingCount=len(warming),
            gap=gap,
            tags=tags,
            neighbours=[
                Neighbour(
                    place=start + k + 1 if row.place else None,
                    name=r.name,
                    address=r.address,
                    returnPct=shown[r.address],
                    trades=r.trades,
                )
                for k, r in enumerate(own[start:start + 5])
            ],
        ),
        share=share_text(row, len(ranked)),
        money=Money(
            returnPct=shown[row.address],
            capital=str(row.capital_raw),
            deposited=str(row.deposited_raw),
            earned=str(row.earned_raw),
            cash=str(row.capital_raw - row.invested_raw - row.unrealized_raw),
            invested=str(row.invested_raw),
            unrealized=str(row.unrealized_raw),
            trendStart=row.trend_start,
            trend=row.trend,
        ),
        positions=Positions(
            count=len(open_),
            mark=_money(sum(p.currentValue for p in open_)),
            sellsFor=_money(sum(p.sellableValue for p in open_)),
            top=listed[:3],
            open=listed,
        ),
        book=book,
        record=record,
        activity=[
            Fill(
                **contexts.get(f.conditionId, MarketContext()).model_dump(),
                at=f.timestamp,
                type=f.type,
                side=f.side if f.side in ("BUY", "SELL") else None,
                outcome=f.outcome or None,
                shares=f.size,
                dollars=_money(f.usdcSize),
                title=f.title,
                icon=f.icon or None,
                category=categories.get(f.conditionId),
                url=f.url,
            )
            for f in fills
            if f.type in ("TRADE", "SPLIT", "MERGE", "REDEEM")
        ],
    )
