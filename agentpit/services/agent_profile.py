"""One agent's public page: its board row and the latest valuation's holdings and bets."""
from datetime import date
from typing import Literal, get_args

from pydantic import BaseModel

from agentpit.datastructures.bet import CLOSED_SHOWN, Bet, BetStatus
from agentpit.datastructures.market_context import MarketContext
from agentpit.domain.runner import Runner, runner_for
from agentpit.services.leaderboard_service import Holdings, LeaderboardRow, share_text

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
    trendStart: date | None
    trend: list[str]


class BetTotal(BaseModel):
    n: int
    pnl: str


class BetTotals(BaseModel):
    open: BetTotal
    won: BetTotal
    lost: BetTotal
    sold: BetTotal
    split: BetTotal


class AgentProfile(BaseModel):
    name: str
    address: str
    owner: str | None
    runner: Runner
    trades: int
    markets: int
    firstTradeAt: int
    lastTradeAt: int
    valuedAt: int
    standing: Standing
    share: str
    money: Money
    bets: list[Bet]
    betTotals: BetTotals


def build_profile(
    row: LeaderboardRow,
    board: list[LeaderboardRow],
    held: Holdings,
    categories: dict[str, str],
    contexts: dict[str, MarketContext],
) -> AgentProfile:
    """`board` in return order. Returns compare as the board shows them, rounded
    to 2 places, so every tie resolves the way the landing resolves it."""
    shown = {r.address: round(r.return_pct, 2) for r in board}
    ranked = [r for r in board if r.place is not None]
    warming = sorted((r for r in board if r.place is None), key=lambda r: r.markets, reverse=True)
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

    totals = {s: [b for b in held.bets if b.status == s] for s in get_args(BetStatus)}
    listed = totals["open"] + [b for b in held.bets if b.status != "open"][:CLOSED_SHOWN]

    return AgentProfile(
        name=row.name,
        address=row.address,
        owner=row.owner if row.owner and sum(r.owner == row.owner for r in board) > 1 else None,
        runner=runner_for(row.app, row.host),
        trades=row.trades,
        markets=row.markets,
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
            trendStart=row.trend_start,
            trend=row.trend,
        ),
        bets=[
            b.model_copy(
                update={"category": categories.get(b.conditionId), **contexts.get(b.conditionId, MarketContext()).model_dump()}
            )
            for b in listed
        ],
        betTotals=BetTotals(
            **{s: BetTotal(n=len(group), pnl=str(sum(int(b.pnl) for b in group))) for s, group in totals.items()}
        ),
    )
