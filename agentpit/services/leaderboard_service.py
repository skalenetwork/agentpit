"""Valuing every trading account on a timer, so ranking never reads the chain."""
import logging
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel

from agentpit.config import Settings
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.onchain.admin import OnchainAdmin
from agentpit.services.account_service import AccountService
from agentpit.services.deployment_reset import reconcile_deployment

log = logging.getLogger(__name__)

TREND_DAYS = 30
DENSE_WINDOW = 48 * 3_600

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


class LeaderboardRow(BaseModel):
    name: str
    address: str
    app: str | None
    capital_raw: int
    deposited_raw: int
    #: Cost basis of the open positions -- what the account put to work.
    invested_raw: int = 0
    #: Mark-to-market gain on those open positions -- profit only on paper.
    unrealized_raw: int = 0
    trades: int
    first_trade_at: int
    last_trade_at: int
    trend: list[str] = []

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
    between two cache refreshes with no change in the underlying data. The
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


class LeaderboardService:
    """Writes one snapshot row per trading account per pass.

    Valuing an account walks its positions on chain, so this cannot happen on
    read: the Arena polls every four seconds, and pagination would not help --
    to know who belongs on page one you must value everyone.
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

    def _capital_invested_unrealized_raw(self, address: str) -> tuple[int, int, int]:
        """`(capital, invested, unrealized)` in base units, from ONE walk.

        Capital is cash plus what the open positions are worth now; invested is
        what they cost; unrealized is the difference. The three together are
        what makes the board legible: $22 up on $1,426 at work is a different
        story from $2 on $35, and neither is money anyone has actually banked.
        """
        cash = self._onchain.usd_balance(address)
        value_whole, cost_whole = self._accounts.value_and_cost(address)
        value_raw = int(round(value_whole * 10**6))
        cost_raw = int(round(cost_whole * 10**6))
        # Subtracting after rounding, so unrealized + invested == value exactly
        # and the columns cannot disagree by a base unit.
        return (cash + value_raw, cost_raw, value_raw - cost_raw)

    def take_snapshot(self, now: int) -> int:
        """Value every trading account. Returns the number of rows written.

        One account failing must not lose the whole pass -- a single unreadable
        position would otherwise cost every other account its data point.
        """
        with self._db.read() as conn:
            accounts = TableRead.list_traded_accounts(conn)

        written = 0
        for account in accounts:
            try:
                capital, invested, unrealized = (
                    self._capital_invested_unrealized_raw(account.eth_address)
                )
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
                        capital,
                        deposited,
                        invested,
                        unrealized,
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
        """Assemble the board from the latest snapshot of each account.

        Reads only the database -- the chain work happened in `take_snapshot`.
        """
        since = datetime.now(UTC).date() - timedelta(days=TREND_DAYS - 1)
        with self._db.read() as conn:
            accounts = TableRead.list_traded_accounts(conn)
            latest = TableRead.latest_account_snapshots(conn)
            tallies = TableRead.count_trades_by_user(conn)
            closes = TableRead.daily_closes(conn, [a.user_id for a in accounts], since)

        rows = []
        for account in accounts:
            snapshot = latest.get(account.user_id)
            tally = tallies.get(account.user_id)
            if snapshot is None or tally is None:
                continue
            capital, deposited, invested, unrealized = snapshot
            rows.append(
                LeaderboardRow(
                    name=display_name(account.handle, account.eth_address),
                    address=account.eth_address,
                    app=account.app,
                    capital_raw=capital,
                    deposited_raw=deposited,
                    invested_raw=invested,
                    unrealized_raw=unrealized,
                    trades=tally.trades,
                    first_trade_at=tally.first_trade_at,
                    last_trade_at=tally.last_trade_at,
                    trend=[
                        str(compute_earned_raw(c.capital, c.deposited))
                        for c in closes.get(account.user_id, [])
                    ],
                )
            )
        return rows
