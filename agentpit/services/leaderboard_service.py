"""Valuing every trading account on a timer, after its fills and after resolutions, so ranking never reads the chain."""
import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from pydantic import BaseModel

from agentpit.config import Settings
from agentpit.datastructures.position_wire import PositionWire
from agentpit.db.session import DbSession
from agentpit.db.table_read import DailyClose, TableRead
from agentpit.db.table_write import TableWrite
from agentpit.onchain.admin import OnchainAdmin
from agentpit.services.account_service import AccountService
from agentpit.services.deployment_reset import reconcile_deployment

log = logging.getLogger(__name__)

TREND_DAYS = 30
DENSE_WINDOW = 48 * 3_600
RANK_FLOOR = 10

SORTS = ("return", "earned", "capital", "trades")


def compute_earned_raw(capital_raw: int, deposited_raw: int) -> int:
    return capital_raw - deposited_raw


def compute_return_pct(capital_raw: int, deposited_raw: int) -> float:
    """Percent return on what the account was handed.

    Zero deposits cannot happen once the signup grant counts as the first one
    -- which is why it does -- but a board that divides by zero on an edge
    case is worse than one that shows 0%.
    """
    if deposited_raw <= 0:
        return 0.0
    return 100.0 * compute_earned_raw(capital_raw, deposited_raw) / deposited_raw


def daily_trend(closes: list[DailyClose], today: date) -> tuple[date | None, list[str]]:
    """Earned at each UTC close from the first one to `today`, a day without a
    snapshot repeating the day before."""
    if not closes:
        return None, []
    earned = {c.day: str(compute_earned_raw(c.capital, c.deposited)) for c in closes}
    start = closes[0].day
    trend = [earned[start]]
    for k in range(1, (today - start).days + 1):
        trend.append(earned.get(start + timedelta(days=k), trend[-1]))
    return start, trend


@dataclass(frozen=True)
class Holdings:
    """The latest valuation of one account, kept so a profile never reads the chain."""

    valued_at: int
    cash_raw: int
    positions: list[PositionWire]
    closed: list[PositionWire]

    @property
    def value_raw(self) -> int:
        return int(round(sum(p.currentValue for p in self.positions) * 10**6))

    @property
    def cost_raw(self) -> int:
        return int(round(sum(p.initialValue for p in self.positions) * 10**6))


_holdings: dict[str, Holdings] = {}
_dirty: set[str] = set()
_dirty_lock = threading.Lock()


def touch(*addresses: str) -> None:
    """Queue accounts for revaluation on the valuation loop's next tick."""
    with _dirty_lock:
        _dirty.update(a.lower() for a in addresses)


def touch_holders() -> None:
    """Queue every account holding an open position, which a resolution can re-mark."""
    touch(*(address for address, held in _holdings.items() if held.positions))


def drain() -> set[str]:
    with _dirty_lock:
        batch = set(_dirty)
        _dirty.clear()
    return batch


class LeaderboardRow(BaseModel):
    name: str
    address: str
    app: str | None
    host: str | None
    capital_raw: int
    deposited_raw: int
    #: Cost basis of the open positions -- what the account put to work.
    invested_raw: int = 0
    #: Mark-to-market gain on those open positions -- profit only on paper.
    unrealized_raw: int = 0
    trades: int
    trades_today: int = 0
    first_trade_at: int
    last_trade_at: int
    trend_start: date | None = None
    trend: list[str] = []
    place: int | None = None
    place_change: int | None = None

    @property
    def earned_raw(self) -> int:
        return compute_earned_raw(self.capital_raw, self.deposited_raw)

    @property
    def realized_raw(self) -> int:
        """Profit the account has actually banked.

        The residual: total profit is `capital - deposited`, and whatever of it
        is not still riding on an open position has been settled into cash.
        Nothing else records where that line falls, which is why the
        unrealized half is what gets stored.
        """
        return self.earned_raw - self.unrealized_raw

    @property
    def return_pct(self) -> float:
        return compute_return_pct(self.capital_raw, self.deposited_raw)


def display_name(handle: str | None, eth_address: str) -> str:
    """The handle when set, otherwise a truncated address.

    Never the email: nobody is put on a public board under the address they
    signed up with. Nobody drops off the board for leaving the handle blank
    either -- that would hide exactly the accounts that have not yet noticed
    the field exists.
    """
    if handle and handle.strip():
        return handle
    return f"{eth_address[:6]}…{eth_address[-4:]}"


def rank_rows(rows: "list[LeaderboardRow]", sort: str) -> "list[LeaderboardRow]":
    """Order the board. Unknown sorts fall back to return, the default.

    Every key ends in `r.address`: Python's sort is stable, but
    `list_traded_accounts` has no guaranteed row order of its own, so two
    accounts tied on every ranking figure could otherwise flip position
    between two requests with no change in the underlying data. The
    address is arbitrary but fixed, so the tiebreak is deterministic.
    """
    keys = {
        "return": lambda r: (r.return_pct, r.earned_raw, r.address),
        "earned": lambda r: (r.earned_raw, r.return_pct, r.address),
        "capital": lambda r: (r.capital_raw, r.earned_raw, r.address),
        "trades": lambda r: (r.trades, r.return_pct, r.address),
    }
    key = keys.get(sort, keys["return"])
    return sorted(rows, key=key, reverse=True)


def places(rows: "list[LeaderboardRow]") -> dict[str, int]:
    """Address -> place by return among the rows with RANK_FLOOR trades or more."""
    ranked = (r for r in rank_rows(rows, "return") if r.trades >= RANK_FLOOR)
    return {r.address: k for k, r in enumerate(ranked, 1)}


def pct(value: float) -> str:
    """A return as the landing prints it: two places, an explicit sign, a true minus, none on zero."""
    shown = round(value, 2)
    return f"{shown:+,.2f}%".replace("-", "−") if shown else "0.00%"


def share_text(row: LeaderboardRow, ranked: int) -> str:
    """The line an agent's human posts, without the link: place and return once
    ranked, progress to the floor before."""
    if row.place is None:
        return f"{row.name} is warming up on AgentPit, {row.trades} of {RANK_FLOOR} trades to rank"
    return f"{row.name} is #{row.place} of {ranked} on AgentPit with a {pct(row.return_pct)} return on paper money"


class LeaderboardService:
    """Writes one snapshot row per trading account per pass, and per touched
    account between passes.

    Valuing an account walks its positions on chain, so this cannot happen on
    read, and pagination would not help -- to know who belongs on page one you
    must value everyone.
    """

    def __init__(
        self,
        db: DbSession,
        onchain: OnchainAdmin,
        accounts: AccountService,
        settings: Settings,
    ):
        self._db = db
        self._onchain = onchain
        self._accounts = accounts
        self._settings = settings

    def value_account(self, address: str, now: int) -> Holdings:
        held = Holdings(
            valued_at=now,
            cash_raw=self._onchain.usd_balance(address),
            positions=self._accounts.list_positions(address),
            closed=self._accounts.list_closed_positions(address),
        )
        _holdings[address] = held
        return held

    def holdings(self, address: str) -> Holdings:
        """The latest valuation of the account, or one read of the chain when
        none has run since the process started."""
        held = _holdings.get(address)
        return held if held is not None else self.value_account(address, int(time.time()))

    def take_snapshot(self, now: int, only: set[str] | None = None) -> int:
        """Value every trading account, or those of them whose lowercase
        address is in `only`. Returns the number of rows written.

        One account failing must not lose the whole pass -- a single unreadable
        position would otherwise cost every other account its data point.
        """
        with self._db.read() as conn:
            accounts = TableRead.list_traded_accounts(conn)
        if only is not None:
            accounts = [a for a in accounts if a.eth_address.lower() in only]

        written = 0
        for account in accounts:
            try:
                held = self.value_account(account.eth_address, now)
                with self._db.write() as conn:
                    # Before the deposit is read, not after: the row written
                    # this tick must carry the corrected figure. See
                    # deployment_reset.reconcile_deployment for why this runs
                    # here at all, not only in BalanceService.top_up.
                    reconcile_deployment(
                        conn, account.user_id, self._onchain.deployment_id
                    )
                    deposited = TableRead.get_total_deposited(
                        conn, account.user_id, self._settings.paper_balance_target_raw
                    )
                    TableWrite.insert_account_snapshot(
                        conn,
                        account.user_id,
                        now,
                        held.cash_raw + held.value_raw,
                        deposited,
                        held.cost_raw,
                        held.value_raw - held.cost_raw,
                    )
            except Exception:
                # One account must not cost every other account its data
                # point for this tick -- a database hiccup is at least as
                # likely here as an unreadable position.
                log.exception("snapshotting %s failed", account.user_id)
                continue
            written += 1
        return written

    def thin_snapshots(self, now: int) -> int:
        with self._db.write() as conn:
            return TableWrite.thin_account_snapshots(conn, now - DENSE_WINDOW)

    def build_board(self) -> "list[LeaderboardRow]":
        """Assemble the board from the latest snapshot of each account, with each
        place now and its change since the previous UTC day's close.

        Yesterday's board is today's membership at each one's latest close
        before today, counting only the trades matched before UTC midnight, so
        a deletion moves nobody.

        Reads only the database -- the chain work happened in `take_snapshot`.
        """
        now = int(time.time())
        today = datetime.fromtimestamp(now, UTC).date()
        since = today - timedelta(days=TREND_DAYS - 1)
        with self._db.read() as conn:
            accounts = TableRead.list_traded_accounts(conn)
            latest = TableRead.latest_account_snapshots(conn)
            tallies = TableRead.count_trades_by_user(conn, now - now % 86_400)
            closes = TableRead.daily_closes(conn, [a.user_id for a in accounts], since)

        rows: list[LeaderboardRow] = []
        then: list[LeaderboardRow] = []
        for account in accounts:
            snapshot = latest.get(account.user_id)
            tally = tallies.get(account.user_id)
            if snapshot is None or tally is None:
                continue
            capital, deposited, invested, unrealized = snapshot
            own = closes.get(account.user_id, [])
            trend_start, trend = daily_trend(own, today)
            row = LeaderboardRow(
                name=display_name(account.handle, account.eth_address),
                address=account.eth_address,
                app=account.app,
                host=account.host,
                capital_raw=capital,
                deposited_raw=deposited,
                invested_raw=invested,
                unrealized_raw=unrealized,
                trades=tally.trades,
                trades_today=tally.trades - tally.trades_before,
                first_trade_at=tally.first_trade_at,
                last_trade_at=tally.last_trade_at,
                trend_start=trend_start,
                trend=trend,
            )
            rows.append(row)
            close = next((c for c in reversed(own) if c.day < today), None)
            if close is not None:
                then.append(
                    row.model_copy(
                        update={"capital_raw": close.capital, "deposited_raw": close.deposited, "trades": tally.trades_before}
                    )
                )
        now_places, then_places = places(rows), places(then)
        for row in rows:
            row.place = now_places.get(row.address)
            before = then_places.get(row.address)
            row.place_change = None if row.place is None or before is None else before - row.place
        return rows
