"""The public board, agent profiles and platform stats, from the database and the latest valuations."""
import time
from datetime import UTC, date, datetime

from fastapi import APIRouter, Query, Response
from pydantic import BaseModel

from agentpit.api.deps import AccountServiceDep, LeaderboardServiceDep, SessionDep
from agentpit.db.table_read import TableRead
from agentpit.liquidity import feed
from agentpit.domain.runner import Runner, runner_for
from agentpit.services.agent_profile import ACTIVITY, AgentProfile, build_profile
from agentpit.services.leaderboard_service import SORTS, rank_rows

router = APIRouter(tags=["leaderboard"])


class LeaderboardEntry(BaseModel):
    rank: int | None
    rankChange: int | None
    name: str
    address: str
    runner: Runner
    capital: str
    earned: str
    #: Cost basis of the open positions -- what the account put to work.
    invested: str
    #: Mark-to-market gain on those open positions -- profit only on paper.
    unrealized: str
    #: Profit actually banked: total minus whatever is still riding.
    realized: str
    returnPct: float
    trades: int
    tradesToday: int
    firstTradeAt: int
    lastTradeAt: int
    trendStart: date | None
    trend: list[str]


class LeaderboardResponse(BaseModel):
    sort: str
    entries: "list[LeaderboardEntry]"


class StatsDay(BaseModel):
    day: date
    agents: int
    active: int
    trades: int
    volume: str


class StatsResponse(BaseModel):
    days: "list[StatsDay]"


@router.get("/leaderboard", response_model=LeaderboardResponse)
def get_leaderboard(
    service: LeaderboardServiceDep,
    sort: str = Query(default="return"),
) -> LeaderboardResponse:
    """Every account that has traded.

    `sort` is one of return, earned, capital, trades; anything else falls back
    to return. It orders the entries only: `rank` is the place by return among
    agents with 10+ trades whatever the sort, null below that, and `rankChange`
    the places gained since the previous UTC day's close. Amounts are base-unit
    integer strings, matching the rest of the API. No email address appears in
    this payload under any sort.
    """
    key = sort if sort in SORTS else "return"
    entries = [
        LeaderboardEntry(
            rank=row.place,
            rankChange=row.place_change,
            name=row.name,
            address=row.address,
            runner=runner_for(row.app, row.host),
            capital=str(row.capital_raw),
            earned=str(row.earned_raw),
            invested=str(row.invested_raw),
            unrealized=str(row.unrealized_raw),
            realized=str(row.realized_raw),
            returnPct=round(row.return_pct, 2),
            trades=row.trades,
            tradesToday=row.trades_today,
            firstTradeAt=row.first_trade_at,
            lastTradeAt=row.last_trade_at,
            trendStart=row.trend_start,
            trend=row.trend,
        )
        for row in rank_rows(service.build_board(), key)
    ]
    return LeaderboardResponse(sort=key, entries=entries)


@router.get("/agents/{address}", response_model=AgentProfile)
def get_agent(
    address: str,
    service: LeaderboardServiceDep,
    accounts: AccountServiceDep,
    db: SessionDep,
) -> AgentProfile | Response:
    """One agent's whole page in one read: the board, the latest valuation's
    holdings and the newest fills. Any letter case; an agent not on the board
    is an empty 404."""
    board = rank_rows(service.build_board(), "return")
    row = next((r for r in board if r.address.lower() == address.lower()), None)
    if row is None:
        return Response(status_code=404)
    held = service.holdings(row.address)
    fills = accounts.list_activity(row.address, limit=ACTIVITY)
    ids = [p.conditionId for p in (*held.positions, *held.closed, *fills)]
    with db.read() as conn:
        categories = TableRead.categories_by_condition_id(conn, ids)
        contexts = TableRead.market_contexts(conn, ids, feed.sided())
    return build_profile(row, board, held, fills, categories, contexts, int(time.time()))


@router.get("/stats", response_model=StatsResponse)
def get_stats(db: SessionDep) -> StatsResponse:
    """Agents, trades and paper volume per UTC day, from the first trade to today."""
    with db.read() as conn:
        activity = TableRead.daily_activity(conn, datetime.now(UTC).date())
    return StatsResponse(
        days=[
            StatsDay(day=a.day, agents=a.agents, active=a.active, trades=a.trades, volume=str(a.volume))
            for a in activity
        ]
    )
