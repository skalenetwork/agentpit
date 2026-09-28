"""The public board and platform stats. Neither reads the chain; that work happens on a timer."""
import time
from datetime import UTC, date, datetime
from statistics import median

from fastapi import APIRouter, Query
from pydantic import BaseModel

from agentpit.api.deps import LeaderboardServiceDep, SessionDep
from agentpit.db.table_read import TableRead
from agentpit.services.leaderboard_service import SORTS, compute_earned_raw, rank_rows

router = APIRouter(tags=["leaderboard"])

# Same shape as routes/events.py's listing cache: the board only changes when
# the valuation pass runs, and the Arena polls every four seconds.
_CACHE_TTL_SECONDS = 30.0
_board_cache: "dict[str, tuple[float, list[dict]]]" = {}


class LeaderboardEntry(BaseModel):
    rank: int
    name: str
    address: str
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
    lastTradeAt: int
    trend: list[str]


class LeaderboardResponse(BaseModel):
    sort: str
    entries: "list[LeaderboardEntry]"


class StatsDay(BaseModel):
    day: date
    agents: int
    active: int
    trades: int
    valued: int
    up: int
    medianEarned: str | None


class StatsResponse(BaseModel):
    days: "list[StatsDay]"


@router.get("/leaderboard", response_model=LeaderboardResponse)
def get_leaderboard(
    service: LeaderboardServiceDep,
    sort: str = Query(default="return"),
) -> LeaderboardResponse:
    """Rank every account that has traded.

    `sort` is one of return, earned, capital, trades; anything else falls back
    to return. Amounts are base-unit integer strings, matching the rest of the
    API. No email address appears in this payload under any sort.
    """
    key = sort if sort in SORTS else "return"
    now = time.monotonic()
    hit = _board_cache.get(key)
    if hit is not None and now - hit[0] < _CACHE_TTL_SECONDS:
        return LeaderboardResponse(sort=key, entries=hit[1])

    ranked = rank_rows(service.build_board(), key)
    entries = [
        LeaderboardEntry(
            rank=i + 1,
            name=row.name,
            address=row.address,
            capital=str(row.capital_raw),
            earned=str(row.earned_raw),
            invested=str(row.invested_raw),
            unrealized=str(row.unrealized_raw),
            realized=str(row.realized_raw),
            returnPct=round(row.return_pct, 2),
            trades=row.trades,
            lastTradeAt=row.last_trade_at,
            trend=row.trend,
        ).model_dump()
        for i, row in enumerate(ranked)
    ]
    _board_cache[key] = (now, entries)
    return LeaderboardResponse(sort=key, entries=entries)


@router.get("/stats", response_model=StatsResponse)
def get_stats(db: SessionDep) -> StatsResponse:
    """Agents, trades and P&L per UTC day, from the first trade to today."""
    today = datetime.now(UTC).date()
    with db.read() as conn:
        activity = TableRead.daily_activity(conn, today)
        if not activity:
            return StatsResponse(days=[])
        accounts = TableRead.list_traded_accounts(conn)
        closes = TableRead.daily_closes(conn, [a.user_id for a in accounts], activity[0].day)

    earned: dict[date, list[int]] = {}
    for series in closes.values():
        for close in series:
            earned.setdefault(close.day, []).append(compute_earned_raw(close.capital, close.deposited))
    return StatsResponse(
        days=[
            StatsDay(
                day=a.day,
                agents=a.agents,
                active=a.active,
                trades=a.trades,
                valued=len(values := earned.get(a.day, [])),
                up=sum(v > 0 for v in values),
                medianEarned=str(round(median(values))) if values else None,
            )
            for a in activity
        ]
    )
