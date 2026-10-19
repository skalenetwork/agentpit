"""Competitions over existing agent accounts: entry, standings, and the start
and end values the capture task takes."""
import logging

from pydantic import BaseModel

from agentpit.config import Settings
from agentpit.datastructures.competition import Competition, Phase
from agentpit.datastructures.position_wire import PositionWire
from agentpit.db.session import DbSession
from agentpit.db.table_read import Entrant, PendingCapture, TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import BusinessRuleError, NotFoundError
from agentpit.domain.runner import Runner, runner_for
from agentpit.liquidity import feed
from agentpit.onchain.admin import OnchainAdmin
from agentpit.services.account_service import AccountService, sellable_against_bids
from agentpit.services.leaderboard_service import RANK_FLOOR, display_name

log = logging.getLogger(__name__)

MINT_LAG = 300
BOOK_GRACE = 600


class Listing(BaseModel):
    competition: Competition
    phase: Phase
    entrants: int


class Standing(BaseModel):
    rank: int | None = None
    name: str
    address: str
    runner: Runner
    listed: bool
    run_starts_at: int
    earned_raw: int
    return_pct: float
    trades: int
    disqualified: bool


class Page(Listing):
    final: bool
    standings: list[Standing]


class Enrollment(BaseModel):
    slug: str
    run_starts_at: int
    ends_at: int

    @classmethod
    def of(cls, competition: Competition, enrolled_at: int) -> "Enrollment":
        return cls(
            slug=competition.slug,
            run_starts_at=max(competition.starts_at, enrolled_at),
            ends_at=competition.ends_at,
        )


def _raw(whole: float) -> int:
    return int(round(whole * 10**6))


def _one(found: list[tuple[Competition, int]], slug: str) -> tuple[Competition, int]:
    if not found:
        raise NotFoundError(f"There is no competition called {slug}.")
    return found[0]


class CompetitionService:
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

    def listing(self, now: int, slug: str | None = None) -> list[Listing]:
        """Every competition, or the one called `slug`, latest start first."""
        with self._db.read() as conn:
            found = TableRead.competitions(conn, slug)
        return [Listing(competition=c, phase=c.phase(now), entrants=n) for c, n in found]

    def page(self, slug: str, now: int) -> Page:
        """The competition with its standings: ranked places by return, then
        earned, then address, then the unranked in the same order, then the
        disqualified. A place needs a start value, RANK_FLOOR run trades and
        no disqualification."""
        with self._db.read() as conn:
            competition, entrants = _one(TableRead.competitions(conn, slug), slug)
            rows = TableRead.entrants(conn, slug)
        standings = sorted(
            ((row, self._standing(row)) for row in rows),
            key=lambda pair: (pair[1].return_pct, pair[1].earned_raw, pair[1].address),
            reverse=True,
        )
        place = 0
        for row, standing in standings:
            if row.start_raw is not None and standing.trades >= RANK_FLOOR and not standing.disqualified:
                place += 1
                standing.rank = place
        phase = competition.phase(now)
        return Page(
            competition=competition,
            phase=phase,
            entrants=entrants,
            final=phase == "ended" and all(r.end_raw is not None for r in rows if r.start_raw is not None),
            standings=sorted((s for _, s in standings), key=lambda s: (s.disqualified, s.rank is None)),
        )

    def _standing(self, row: Entrant) -> Standing:
        base = max(row.start_raw or 0, self._settings.paper_balance_target_raw)
        return Standing(
            name=display_name(row.handle, row.eth_address),
            address=row.eth_address,
            runner=runner_for(row.app, row.host),
            listed=row.listed,
            run_starts_at=row.run_starts_at,
            earned_raw=row.earned_raw,
            return_pct=round(100 * row.earned_raw / base, 2),
            trades=row.trades,
            disqualified=row.disqualified,
        )

    def enroll(self, user_id: str, slug: str, now: int, switch: bool) -> Enrollment:
        """Enter the agent, or return its entry unchanged when it is already
        the owner's entry. Before the lock it replaces the owner's other agent
        only with `switch`; from the lock only an owner with no entry can enter,
        with an agent holding just cash."""
        with self._db.read() as conn:
            competition, _ = _one(TableRead.competitions(conn, slug), slug)
            seat = TableRead.seat(conn, slug, user_id)
        if seat is not None and seat.enrolled_at is not None:
            return Enrollment.of(competition, seat.enrolled_at)
        if seat is None or seat.owner is None:
            raise BusinessRuleError(
                "Only an agent that is set up, belongs to a person and never moved apUSD "
                "wallet to wallet can enter a competition."
            )
        phase = competition.phase(now)
        if phase == "ended":
            raise BusinessRuleError(f"{competition.title} has ended.")
        if phase == "upcoming":
            if seat.taken and not switch:
                raise BusinessRuleError(
                    f"Another of your agents is entered in {competition.title}. "
                    "Switch agents in the AgentPit console."
                )
        elif seat.taken:
            raise BusinessRuleError(
                f"Another of your agents is entered in {competition.title}, and agents cannot be switched after the lock."
            )
        elif sum(p.currentValue for p in self._accounts.list_positions(seat.eth_address) if not p.settled) >= 1:
            raise BusinessRuleError(
                f"Since the lock only an agent holding just cash can enter {competition.title}. "
                "Close its open positions first."
            )
        with self._db.write() as conn:
            entered = TableWrite.enter_competition(
                conn, slug, seat.owner, user_id, now, switch and phase == "upcoming"
            )
        return Enrollment.of(competition, now) if entered else self.enroll(user_id, slug, now, switch)

    def leave(self, user_id: str, slug: str, now: int) -> None:
        with self._db.read() as conn:
            competition, _ = _one(TableRead.competitions(conn, slug), slug)
        if competition.phase(now) != "upcoming":
            raise BusinessRuleError(
                f"Entries in {competition.title} are final from the lock, 24 hours before the start."
            )
        with self._db.write() as conn:
            TableWrite.leave_competition(conn, slug, user_id)

    def entered(self, owner: str, now: int) -> dict[str, list[str]]:
        """user_id -> the slugs of the competitions not yet ended the owner entered it in."""
        with self._db.read() as conn:
            return TableRead.entered_by_owner(conn, owner, now)

    def save(self, competition: Competition, now: int) -> Listing:
        with self._db.write() as conn:
            saved = TableWrite.save_competition(conn, competition, now)
        if not saved:
            raise BusinessRuleError(f"{competition.slug} has started, so its start and end cannot change.")
        return self.listing(now, competition.slug)[0]

    def capture(self, now: int) -> None:
        """Take every start and end value that is due; NULL is pending, so a
        failed or skipped capture is retried on the next call. A start waits
        MINT_LAG after a top-up claim for its mint to land. Nothing is taken
        while the mirror is still loading its targets."""
        if self._settings.liquidity_engine_enabled and (feed.HOUSE is None or not feed.HOUSE.state.by_asset):
            return
        with self._db.read() as conn:
            pending = TableRead.pending_captures(conn, now, now - MINT_LAG)
        for entry in pending:
            try:
                self._capture(entry, now)
            except Exception:
                log.exception("capturing %s in %s failed", entry.eth_address, entry.slug)

    def _capture(self, entry: PendingCapture, now: int) -> None:
        """Cash plus positions: at the board's marks for the start, at what
        would sell for the end. Cash read again after the positions; a change
        means a fill landed in between, so the value waits for the next call."""
        ending = entry.start_raw is not None
        cash = self._onchain.usd_balance(entry.eth_address)
        worth = 0.0
        for position in self._accounts.list_positions(entry.eth_address):
            value = self._exit_value(entry, position, now) if ending else position.currentValue
            if value is None:
                return
            worth += value
        if self._onchain.usd_balance(entry.eth_address) != cash:
            return
        edge, boundary = ("END", entry.ends_at) if ending else ("START", entry.run_starts_at)
        value_raw = cash + _raw(worth)
        with self._db.write() as conn:
            if TableWrite.capture_entry(conn, entry.slug, entry.user_id, edge, value_raw):
                log.info(
                    "Competition %s %s value for %s: %d raw, %ds past the boundary",
                    entry.slug, edge.lower(), entry.eth_address, value_raw, now - boundary,
                )

    @staticmethod
    def _exit_value(entry: PendingCapture, position: PositionWire, now: int) -> float | None:
        """Settled and unmirrored positions at their mark, mirrored ones by
        walking Polymarket's whole bid side; asks without bids are worth 0.
        None while the book is missing, until BOOK_GRACE after the end."""
        if position.settled or feed.quote(position.asset) is None:
            return position.currentValue
        book = feed.book(position.asset)
        if book is None:
            if now < entry.ends_at + BOOK_GRACE:
                return None
            log.warning(
                "Competition %s end value for %s: no book for %s, valued at its mark",
                entry.slug, entry.eth_address, position.asset,
            )
            return position.currentValue
        if book.bids:
            return sellable_against_bids(list(book.bids), _raw(position.size), floor=False).value
        return 0.0 if book.asks else position.currentValue
