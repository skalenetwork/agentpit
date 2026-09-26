"""The public board. Served from memory; the chain work happens on a timer."""
import time

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel

from agentpit.api.deps import LeaderboardServiceDep, SessionDep
from agentpit.db.table_read import TableRead
from agentpit.services.leaderboard_service import (
    SORTS,
    compute_earned_raw,
    compute_return_pct,
    downsample,
    rank_rows,
)

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
    trend: list[float]


class LeaderboardResponse(BaseModel):
    sort: str
    entries: "list[LeaderboardEntry]"


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
            trend=row.trend,
        ).model_dump()
        for i, row in enumerate(ranked)
    ]
    _board_cache[key] = (now, entries)
    return LeaderboardResponse(sort=key, entries=entries)


# 7 days at the 5-minute valuation cadence, thinned to what a 72-pixel
# sparkline can show. Fetching a bounded window and thinning it beats sending
# 8,640 points the client immediately discards.
_HISTORY_ROWS = 2_016
_HISTORY_POINTS = 60


class HistoryPoint(BaseModel):
    t: int
    capital: str
    earned: str
    returnPct: float


class HistoryResponse(BaseModel):
    points: "list[HistoryPoint]"


@router.get("/leaderboard/{address}/history", response_model=HistoryResponse)
def get_leaderboard_history(
    db: SessionDep,
    address: str = Path(...),
) -> HistoryResponse:
    """One account's equity curve, for the sparkline on its board row.

    Return rather than a bare balance, because return is what the board ranks
    on by default and a curve that disagreed with the column beside it would
    be worse than no curve. Public and database-only, like the board itself --
    and carrying no email, for the same reason.

    The address must match what `GET /leaderboard` returned; that is the only
    caller, and it passes the stored string back verbatim.
    """
    with db.read() as conn:
        user = TableRead.get_user_by_eth_address(conn, address)
        if user is None:
            raise HTTPException(status_code=404, detail="no such account")
        rows = TableRead.list_account_snapshots(
            conn, user.user_id, _HISTORY_ROWS
        )

    return HistoryResponse(
        points=[
            HistoryPoint(
                t=t,
                capital=str(capital),
                earned=str(compute_earned_raw(capital, deposited)),
                returnPct=round(compute_return_pct(capital, deposited), 2),
            )
            for t, capital, deposited in downsample(rows, _HISTORY_POINTS)
        ]
    )
