"""Competitions over existing agent accounts: the files that define them,
entry, the race every surface reads, and the hourly points and the start and
end values the competition loop takes."""
import logging
import threading
import time
from bisect import bisect_right
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import groupby, pairwise
from operator import itemgetter
from pathlib import Path
from typing import Literal

import psycopg
from pydantic import BaseModel

from agentpit.config import Settings
from agentpit.datastructures.competition import (
    FINAL_DAY,
    FIXTURES,
    HOUR,
    LEADERS,
    LINES,
    PLACE_FLOOR,
    POINT_LAG,
    RECAP,
    TAPE,
    Competition,
    Entry,
    Fixture,
    Line,
    Phase,
    Race,
    RaceEntry,
    RaceHead,
    RaceSummary,
    Score,
    Series,
    Stake,
    Standing,
    TapeFill,
    load_competition,
    score,
)
from agentpit.datastructures.match_leg import MICRO, legs_for_user
from agentpit.datastructures.position_wire import PositionWire
from agentpit.db.session import DbSession
from agentpit.db.table_read import PendingCapture, TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import BusinessRuleError, NotFoundError
from agentpit.domain.runner import runner_for
from agentpit.liquidity import feed
from agentpit.liquidity.replica import BookSnapshot
from agentpit.services.account_service import AccountService, sellable_against_bids
from agentpit.services.leaderboard_service import (
    DENSE_WINDOW,
    Holdings,
    all_holdings,
    display_name,
    pct,
    peer_next_block,
)

log = logging.getLogger(__name__)

MINT_LAG = 300
END_DEADLINE = 3 * HOUR
RACE_TTL = 15
FOLDER = Path(__file__).resolve().parents[2] / "competitions"

Exit = BookSnapshot | float
Key = tuple[int, int, str]


@dataclass(frozen=True)
class _Series:
    """Each eligible entrant's place at every column so far, each column's
    leader, and the key of the entries the columns were ranked from."""

    at: list[int]
    key: frozenset[tuple[str, int | None]]
    ranks: dict[str, list[int | None]]
    leaders: list[str | None]


@dataclass(frozen=True)
class RaceBuild:
    """One build of a race: the Race every surface serves, its summary, each
    entrant's entry by lowercase address and the places behind each chart
    line by address."""

    race: Race
    summary: RaceSummary
    entries: dict[str, RaceEntry]
    ranks: dict[str, list[int | None]]


_cursor: dict[str, int] = {}
_bells: dict[str, dict[str, Exit]] = {}
_overdue: set[tuple[str, str]] = set()
_series: dict[str, _Series] = {}
_archived: dict[str, RaceBuild] = {}
_races: tuple[int, dict[str, RaceBuild]] | None = None
_races_lock = threading.Lock()


class Enrollment(BaseModel):
    slug: str
    title: str
    run_starts_at: int
    ends_at: int

    @classmethod
    def of(cls, competition: Competition, enrolled_at: int) -> "Enrollment":
        return cls(
            slug=competition.slug,
            title=competition.title,
            run_starts_at=max(competition.starts_at, enrolled_at),
            ends_at=competition.ends_at,
        )

    def line(self, name: str, now: int) -> str:
        """The sentence the agent relays about its entry."""
        if now < self.run_starts_at:
            return f"{name} is entered in {self.title}, starting {_date(self.run_starts_at)}"
        return f"{name} joined {self.title}; its run ends {_date(self.ends_at)}"


def invalidate() -> None:
    """Drop the shared build, so the next read sees an entry or a leave."""
    global _races
    with _races_lock:
        _races = None


def _raw(whole: float) -> int:
    return int(round(whole * 10**6))


def _one(found: list[tuple[Competition, int | None]], slug: str) -> tuple[Competition, int | None]:
    if not found:
        raise NotFoundError(f"There is no competition called {slug}.")
    return found[0]


def _date(ts: int) -> str:
    at = datetime.fromtimestamp(ts, UTC)
    return f"{at:%b} {at.day} at {at:%H:%M} UTC"


def _left(seconds: int) -> str:
    days, rest = divmod(seconds, 86_400)
    hours, rest = divmod(rest, HOUR)
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h {rest // 60}m" if hours else f"{rest // 60}m"


def _dollars(raw: int) -> str:
    return f"${raw / MICRO:,.0f}"


def _agents(n: int) -> str:
    return f"{n} agent" if n == 1 else f"{n} agents"


def _race_line(c: Competition, phase: Phase, final: bool, top: list[Standing], entrants: int, now: int) -> str:
    if phase in ("upcoming", "locked"):
        return f"{c.title} starts {_date(c.starts_at)} with {_agents(entrants)} entered"
    if phase == "live" and not top:
        return f"{c.title} is live with {_left(c.ends_at - now)} left and no agent placed yet"
    if phase == "live":
        ahead = f", {_dollars(top[1].behind or 0)} ahead of {top[1].name}" if len(top) > 1 else ""
        return f"{top[0].name} leads {c.title} at {pct(top[0].returnPct)}{ahead}, {_left(c.ends_at - now)} left"
    if not final:
        return f"{c.title} has ended; final values at the bids are coming in"
    if not top:
        return f"{c.title} ended with no agent placed"
    return f"{top[0].name} won {c.title} with {pct(top[0].returnPct)}, {_agents(len(top))} placed"


def _agent_line(head: RaceHead, s: Standing, below: Standing | None, started: bool, now: int) -> str:
    if head.phase in ("upcoming", "locked"):
        return f"{s.name} is entered in {head.title}, starting {_date(head.startsAt)}"
    if s.disqualified:
        return f"{s.name} is disqualified from {head.title}"
    if head.phase == "ended" and s.rank is None:
        return f"{s.name} did not place in {head.title}"
    if head.phase == "ended" and head.final:
        return f"{s.name} finished #{s.rank} of {head.ranked} in {head.title} with {pct(s.returnPct)}"
    if head.phase == "ended":
        return f"{s.name} was #{s.rank} of {head.ranked} at the bell of {head.title}; final values are coming in"
    if not started:
        return f"{s.name} has joined {head.title}; its start value is being taken"
    if s.rank is None:
        would = "" if s.wouldRank is None else f"; it would be #{s.wouldRank} now"
        return f"{s.name} needs one trade to place in {head.title}{would}"
    if s.rank == 1:
        gap = f", {_dollars(below.behind or 0)} ahead of #2" if below else ""
        place = f"{s.name} leads {head.title} at {pct(s.returnPct)}{gap}"
    else:
        place = (
            f"{s.name} is #{s.rank} of {head.ranked} in {head.title} at {pct(s.returnPct)}, "
            f"{_dollars(s.behind or 0)} behind #{s.rank - 1}"
        )
    bids = f"; at the bids now it would be #{s.bids.rank}" if s.bids and s.bids.rank not in (None, s.rank) else ""
    return f"{place}, {_left(head.endsAt - now)} left{bids}"


def exit_state(position: PositionWire) -> Exit | None:
    """What the position sells into now: a price per share, its payout once
    settled and its mark when unmirrored or on an empty book, else its book.
    None while a mirrored book is missing."""
    if position.settled or feed.quote(position.asset) is None:
        return position.curPrice
    book = feed.book(position.asset)
    if book is None:
        return None
    return book if book.bids or book.asks else position.curPrice


def exit_value(position: PositionWire, state: Exit) -> float:
    """The position sold into `state`: down the whole bid side of a book,
    which is 0 with asks only, else at the price per share."""
    if isinstance(state, BookSnapshot):
        return sellable_against_bids(list(state.bids), _raw(position.size), floor=False).value
    return state * position.size


def _freeze(entries: list[PendingCapture], now: int) -> dict[str, Exit]:
    """What each token the entries held at their last valuation sells into
    now, for every end value of their competition to share."""
    held = all_holdings()
    bell: dict[str, Exit] = {}
    for entry in entries:
        if (holdings := held.get(entry.eth_address)) is None:
            continue
        for position in holdings.positions:
            if position.asset not in bell and (state := exit_state(position)) is not None:
                bell[position.asset] = state
    log.info(
        "Competition %s bell state frozen for %d tokens, %ds after the end",
        entries[0].slug, len(bell), now - entries[0].ends_at,
    )
    return bell


def _places(keys: Iterable[Key]) -> dict[str, int]:
    """Address -> place, best key first."""
    return {address: place for place, (_, _, address) in enumerate(sorted(keys, reverse=True), 1)}


def _eligible_key(eligible: list[Entry], last: int) -> frozenset[tuple[str, int | None]]:
    """What the columns up to `last` are ranked from: the entries with a point
    by then, each with its first run trade if that came before `last`."""
    return frozenset(
        (e.user_id, e.first_trade_at if e.first_trade_at is not None and e.first_trade_at < last else None)
        for e in eligible
        if e.start is not None and e.start.at < last
    )


def _series_of(
    conn: psycopg.Connection, slug: str, eligible: list[Entry], key: Callable[[str, int], Key]
) -> _Series:
    """The competition's columns, each with the places of the eligible
    entries that have a point there and traded before it. New columns are
    appended to the cached ones, and every column is ranked again when the
    entries the cached ones were ranked from changed."""
    cached = _series.get(slug)
    if cached is not None and cached.key != _eligible_key(eligible, cached.at[-1] if cached.at else 0):
        cached = None
    rows = TableRead.points(conn, slug, cached.at[-1] if cached is not None and cached.at else 0)
    if cached is not None and not rows:
        return cached
    at = [] if cached is None else [*cached.at]
    ranks = {} if cached is None else {address: [*line] for address, line in cached.ranks.items()}
    leaders = [] if cached is None else [*cached.leaders]
    by_user = {e.user_id: e for e in eligible}
    for t, column in groupby(rows, key=itemgetter(0)):
        placed = _places(
            key(e.address, value)
            for _, user_id, value in column
            if (e := by_user.get(user_id)) is not None and e.first_trade_at is not None and e.first_trade_at < t
        )
        for address in placed.keys() - ranks.keys():
            ranks[address] = [None] * len(at)
        for address, line in ranks.items():
            line.append(placed.get(address))
        at.append(t)
        leaders.append(next(iter(placed), None))
    _series[slug] = _Series(at, _eligible_key(eligible, at[-1] if at else 0), ranks, leaders)
    return _series[slug]


def _bids(slug: str, entries: list[Entry], held: dict[str, Holdings]) -> dict[str, int]:
    """Each started entry's value if the race ended now: its end value once
    taken, else its latest valuation's cash plus every position sold into its
    token's bell state, its live state without one, or its mark while its book
    is missing. None without a valuation since its start value."""
    bell = _bells.get(slug, {})
    states: dict[str, Exit] = {}
    values: dict[str, int] = {}
    for e in entries:
        if e.end is not None:
            values[e.address] = e.end.raw
        elif e.start is not None and (h := held.get(e.address)) is not None and h.valued_at >= e.start.at:
            for p in h.positions:
                if p.asset not in states:
                    state = bell[p.asset] if p.asset in bell else exit_state(p)
                    states[p.asset] = p.curPrice if state is None else state
            values[e.address] = h.cash_raw + _raw(sum(exit_value(p, states[p.asset]) for p in h.positions))
    return values


def _fixtures(
    conn: psycopg.Connection,
    c: Competition,
    eligible: list[Entry],
    held: dict[str, Holdings],
    official: dict[str, int],
    places: Callable[[dict[str, int]], dict[str, int]],
) -> list[Fixture]:
    """The markets eligible entrants hold that are still to settle before the
    end and where some outcome would move some place, soonest first with an
    event's markets together. Each stake carries its holder's place if each
    outcome paid $1 and the other nothing, everyone else unchanged."""
    held_in: dict[str, list[tuple[Entry, PositionWire]]] = {}
    for e in eligible:
        for p in held[e.address].positions if e.address in held else []:
            if not p.settled:
                held_in.setdefault(p.conditionId, []).append((e, p))
    yes = {p.asset if p.outcomeIndex == 0 else p.oppositeAsset for pairs in held_in.values() for _, p in pairs}
    sided = [token for token in yes if (book := feed.book(token)) is not None and book.bids and book.asks]
    contexts = TableRead.market_contexts(conn, list(held_in), sided)
    due = {
        cid: when
        for cid, ctx in contexts.items()
        if ctx.resolvedAt is None and (when := ctx.kickoff or ctx.endDate) is not None and when < c.ends_at
    }
    event = {cid: held_in[cid][0][1].eventSlug or cid for cid in due}
    soonest: dict[str, int] = {}
    for cid, when in due.items():
        soonest[event[cid]] = min(when, soonest.get(event[cid], when))
    placed = places(official)
    fixtures: list[Fixture] = []
    for cid in sorted(due, key=lambda cid: (soonest[event[cid]], event[cid], due[cid], cid)):
        if len(fixtures) == FIXTURES:
            break
        first = held_in[cid][0][1]
        outcomes = [first.outcome, first.oppositeOutcome] if first.outcomeIndex == 0 else [first.oppositeOutcome, first.outcome]
        scenarios = []
        for outcome in range(len(outcomes)):
            values = dict(official)
            for e, p in held_in[cid]:
                values[e.address] += (_raw(p.size) if p.outcomeIndex == outcome else 0) - _raw(p.currentValue)
            scenarios.append(places(values))
        stakes = [
            Stake(
                address=e.address,
                name=display_name(e.handle, e.address),
                outcome=p.outcomeIndex,
                shares=p.size,
                value=_raw(p.currentValue),
                places=[scenario.get(e.address) for scenario in scenarios],
            )
            for e, p in sorted(held_in[cid], key=lambda pair: -pair[1].currentValue)
        ]
        if any(place != placed.get(stake.address) for stake in stakes for place in stake.places):
            fixtures.append(
                Fixture(
                    **contexts[cid].model_dump(),
                    market=first.slug,
                    question=first.title,
                    url=first.url,
                    outcomes=outcomes,
                    stakes=stakes,
                )
            )
    return fixtures


def _tape(conn: psycopg.Connection, c: Competition, started: list[Entry]) -> list[TapeFill]:
    """The newest TAPE run fills and payouts of the started entrants, newest
    first; consecutive fills by one agent on one market, side and outcome
    within an hour of the newest are one row priced at their average."""
    names = {e.api_key: (e.address, display_name(e.handle, e.address)) for e in started}
    rows = TableRead.recent_activity(conn, [(e.api_key, e.run_starts_at, c.ends_at) for e in started], TAPE)
    tape: list[TapeFill] = []
    shares: list[int] = []
    for row, m in rows:
        address, name = names[row["API_KEY"]]
        at = row["MATCH_TIME"]
        if row["PAID"] is not None:
            tape.append(
                TapeFill(
                    at=at, address=address, name=name, kind="payout", market=m.slug, question=m.question,
                    outcome=m.winner, price=None, usd=row["PAID"], fills=1,
                )
            )
            shares.append(0)
            continue
        for leg in legs_for_user(row, row["API_KEY"]):
            kind = "buy" if leg.side == "BUY" else "sell"
            outcome = dict(m.erc1155_tokens)[leg.token_id]
            usd = leg.price_micro * leg.size_micro // MICRO
            last = tape[-1] if tape else None
            if (
                last is not None
                and (last.address, last.kind, last.market, last.outcome) == (address, kind, m.slug, outcome)
                and last.at - at < HOUR
            ):
                shares[-1] += leg.size_micro
                tape[-1] = last.model_copy(
                    update={"usd": last.usd + usd, "price": round((last.usd + usd) / shares[-1], 3), "fills": last.fills + 1}
                )
            else:
                tape.append(
                    TapeFill(
                        at=at, address=address, name=name, kind=kind, market=m.slug, question=m.question,
                        outcome=outcome, price=round(leg.price_micro / MICRO, 3), usd=usd, fills=1,
                    )
                )
                shares.append(leg.size_micro)
    return tape[:TAPE]


class CompetitionService:
    def __init__(self, db: DbSession, accounts: AccountService, settings: Settings):
        self._db = db
        self._accounts = accounts
        self._settings = settings

    def races(self, now: int) -> dict[str, RaceBuild]:
        """Every race, latest start first, from one build every caller shares
        until RACE_TTL has passed or a competition reaches its lock, start,
        final day or end."""
        global _races
        with _races_lock:
            if _races is None or now >= _races[0]:
                _races = self._build(now)
            return _races[1]

    def _build(self, now: int) -> tuple[int, dict[str, RaceBuild]]:
        """Every race read in one transaction, and when the build expires. A
        race final and ended over RECAP ago is built once per process."""
        held = all_holdings()
        with self._db.read() as conn:
            found = TableRead.competitions(conn)
            entries: dict[str, list[Entry]] = {}
            for entry in TableRead.entries(conn, [c.slug for c, _ in found if c.slug not in _archived]):
                entries.setdefault(entry.slug, []).append(entry)
            built = {
                c.slug: _archived.get(c.slug)
                or self._race(conn, c, final_at is not None, entries.get(c.slug, []), held, now)
                for c, final_at in found
            }
        for c, final_at in found:
            if final_at is not None and c.ends_at < now - RECAP:
                _archived[c.slug] = built[c.slug]
        turns = [t for c, _ in found for t in (c.locks_at, c.starts_at, c.ends_at - FINAL_DAY, c.ends_at) if t > now]
        return min([now + RACE_TTL, *turns]), built

    def _race(
        self,
        conn: psycopg.Connection,
        c: Competition,
        final: bool,
        entries: list[Entry],
        held: dict[str, Holdings],
        now: int,
    ) -> RaceBuild:
        """The race on its official basis: each value while live, the value at
        the bell until final, then the end values. A place needs a start
        value, PLACE_FLOOR run trades and no disqualification, and every place
        in every scenario comes from one key: return in basis points, then
        earned, then address."""
        phase = c.phase(now)
        target = self._settings.paper_balance_target_raw
        starts = {e.address: e.start for e in entries if e.start is not None}
        eligible = [e for e in entries if e.address in starts and not e.disqualified]
        ranked = [e.address for e in eligible if e.trades >= PLACE_FLOOR]
        bell = {e.address: s.raw if e.value_raw is None else e.value_raw for e in entries if (s := e.start) is not None}
        official = {
            e.address: e.end.raw if final and e.end is not None else bell[e.address] for e in entries if e.address in bell
        }

        def base(address: str) -> int:
            return max(starts[address].raw, target)

        def key(address: str, value: int) -> Key:
            earned = value - starts[address].raw
            return score(earned, base(address)), earned, address

        def places(values: dict[str, int]) -> dict[str, int]:
            return _places(key(a, values[a]) for a in ranked if a in values)

        def scored(address: str, values: dict[str, int], at: dict[str, int]) -> Score:
            bp, earned, _ = key(address, values[address])
            return Score(rank=at.get(address), earned=earned, returnPct=bp / 100)

        keys = {a: key(a, value) for a, value in official.items()}
        placed = _places(keys[a] for a in ranked)
        order = list(placed)
        series = _series_of(conn, c.slug, eligible, key)
        bids = _bids(c.slug, entries, held) if c.ends_at - FINAL_DAY <= now and not final else {}
        at_bids, at_bell = places(bids), places(bell)
        then = bisect_right(series.at, now - now % 86_400) - 1
        led = Counter(leader for t, leader in zip(series.at, series.leaders) if leader is not None and t < c.ends_at)

        def standing(e: Entry) -> Standing:
            a = e.address
            own = keys.get(a, (0, 0, a))
            bp, earned, _ = own
            rank = placed.get(a)
            behind = would = None
            if rank is not None and rank > 1:
                up = order[rank - 2]
                behind = max(0, -(-keys[up][1] * base(a) // base(up)) - earned)
            if phase == "live" and a in starts and not e.disqualified and e.trades == 0 and official[a] >= MICRO:
                would = 1 + sum(keys[r] > own for r in ranked)
            was = series.ranks[a][then] if phase == "live" and then >= 0 and a in series.ranks else None
            return Standing(
                address=a,
                name=display_name(e.handle, a),
                runner=runner_for(e.app, e.host),
                listed=e.listed,
                runStartsAt=e.run_starts_at,
                endAt=None if e.end is None else e.end.at,
                trades=e.trades,
                lastTradeAt=e.last_trade_at,
                disqualified=e.disqualified,
                rank=rank,
                earned=earned,
                returnPct=bp / 100,
                rankChange=None if rank is None or was is None else was - rank,
                behind=behind,
                wouldRank=would,
                hoursLed=led[a],
                bids=scored(a, bids, at_bids) if a in bids else None,
                bell=scored(a, bell, at_bell) if phase == "ended" and a in bell else None,
            )

        standings = sorted(
            sorted(map(standing, entries), key=lambda s: (s.returnPct, s.earned, s.address), reverse=True),
            key=lambda s: (s.disqualified, s.rank is None),
        )
        top = standings[: len(placed)]
        shown = {s.address for s in top[:LINES]} | {leader for leader in series.leaders if leader is not None}
        leaders = [leader for leader in [*series.leaders, *order[:1]] if leader is not None]
        head = RaceHead(
            slug=c.slug,
            title=c.title,
            startsAt=c.starts_at,
            endsAt=c.ends_at,
            phase=phase,
            final=final,
            prizes=c.prizes,
            entrants=len(entries),
            ranked=len(top),
            line=_race_line(c, phase, final, top, len(entries), now),
        )
        race = Race(
            **dict(head),
            builtAt=now,
            leadChanges=sum(a != b for a, b in pairwise(leaders)),
            standings=standings,
            series=Series(
                at=series.at,
                lines=[
                    Line(address=s.address, ranks=[*series.ranks.get(s.address, [None] * len(series.at)), s.rank])
                    for s in standings
                    if s.address in shown
                ],
            ),
            fixtures=_fixtures(conn, c, eligible, held, official, places) if phase == "live" else [],
            tape=_tape(conn, c, [e for e in entries if e.address in starts]) if phase == "live" else [],
        )

        def entry(s: Standing) -> RaceEntry:
            above = top[s.rank - 2] if s.rank is not None and s.rank > 1 else None
            below = top[s.rank] if s.rank is not None and s.rank < len(top) else None
            line = _agent_line(head, s, below, s.address in starts, now)
            return RaceEntry(race=head, standing=s, above=above, below=below, line=line)

        return RaceBuild(
            race=race,
            summary=RaceSummary(**dict(head), standings=standings[:LEADERS]),
            entries={s.address.lower(): entry(s) for s in standings},
            ranks=series.ranks,
        )

    def enroll(self, user_id: str, slug: str, now: int, switch: bool) -> Enrollment:
        """Enter the agent, or return its entry unchanged when it is already
        the owner's entry. Before the lock it replaces the owner's other agent
        only with `switch`; from the lock only an owner with no entry can enter,
        with an agent holding just cash. Nobody enters in the final hour, so
        every entry gets its start value."""
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
        if now >= competition.ends_at - HOUR:
            raise BusinessRuleError(f"{competition.title} takes no new entries in its final hour.")
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
        elif sum(p.currentValue for p in self._accounts.wallet(seat.eth_address).positions if not p.settled) >= 1:
            raise BusinessRuleError(
                f"Since the lock only an agent holding just cash can enter {competition.title}. "
                "Close its open positions first."
            )
        with self._db.write() as conn:
            entered = TableWrite.enter_competition(
                conn, slug, seat.owner, user_id, now, switch and phase == "upcoming"
            )
        if not entered:
            return self.enroll(user_id, slug, now, switch)
        invalidate()
        return Enrollment.of(competition, now)

    def leave(self, user_id: str, slug: str, now: int) -> None:
        with self._db.read() as conn:
            competition, _ = _one(TableRead.competitions(conn, slug), slug)
        if competition.phase(now) != "upcoming":
            raise BusinessRuleError(
                f"Entries in {competition.title} are final from the lock, 24 hours before the start."
            )
        with self._db.write() as conn:
            TableWrite.leave_competition(conn, slug, user_id)
        invalidate()

    @staticmethod
    def sync(conn: psycopg.Connection, folder: Path, now: int) -> None:
        """Save the competition of every *.toml file in `folder`, then delete
        the competitions with no file. A bad file, new prizes from the lock or
        a new window from the start is logged and leaves its row as it was. A
        competition with no file that has started or has entries is logged
        and kept. A missing folder changes nothing."""
        if not folder.is_dir():
            log.error("Competitions folder %s is missing, so competitions were not synced", folder)
            return
        paths = sorted(folder.glob("*.toml"))
        for path in paths:
            try:
                competition = load_competition(path)
            except (OSError, ValueError) as e:
                log.error("Competition file %s refused, its competition left as it was: %s", path.name, e)
                continue
            if not TableWrite.save_competition(conn, competition, now):
                log.error(
                    "Competition file %s refused: the prizes of %s are fixed from the lock, its start and end from the start",
                    path.name, competition.slug,
                )
        for slug in TableWrite.delete_competitions(conn, [path.stem for path in paths], now):
            log.error("Competition %s has no file but has started or has entries, so it was kept", slug)

    def tick(self, now: int) -> None:
        """One pass of the competition loop: the points due, then the values
        due, then the competitions that turned final."""
        self.record(now)
        self.capture(now)
        with self._db.write() as conn:
            final = TableWrite.stamp_final(conn, now, peer_next_block())
        for slug in final:
            _bells.pop(slug, None)
            log.info("Competition %s is final", slug)

    def record(self, now: int) -> None:
        """Write the points due in every competition: at each whole UTC hour
        inside its run, then at its end, from POINT_LAG after it and back to
        an hour inside the snapshots thinning keeps whole. Each competition's
        latest boundary written is read once per process."""
        floor = now - DENSE_WINDOW + HOUR
        with self._db.read() as conn:
            recent = [c for c, _ in TableRead.competitions(conn) if c.starts_at < now and c.ends_at >= floor]
            for competition in recent:
                if competition.slug not in _cursor:
                    _cursor[competition.slug] = TableRead.last_point(conn, competition.slug)
        until = now - POINT_LAG
        for competition in recent:
            after = max(_cursor[competition.slug], competition.starts_at, floor - 1)
            ts = list(range((after // HOUR + 1) * HOUR, min(until + 1, competition.ends_at), HOUR))
            if after < competition.ends_at <= until:
                ts.append(competition.ends_at)
            if not ts:
                continue
            with self._db.write() as conn:
                written = TableWrite.write_points(conn, competition.slug, ts)
            _cursor[competition.slug] = ts[-1]
            log.info(
                "Competition %s points at %d boundaries up to %d: %d rows",
                competition.slug, len(ts), ts[-1], written,
            )

    def capture(self, now: int) -> None:
        """Take every start and end value that is due; NULL is pending, so a
        failed or skipped capture is retried on the next call. Ends come
        first, and the first call at or after an end freezes what each token
        its entrants hold sells into, for all of its end values. An end is
        taken once its bell point is written and no fill of the account is
        PENDING; still missing an HOUR after the end it is logged as an error,
        and from END_DEADLINE it is the bell point's value, so the pause it
        holds always lifts. A start waits MINT_LAG after a top-up claim for
        its mint to land. Nothing is taken until the mirror is ready."""
        if not feed.ready(self._settings.liquidity_engine_enabled):
            return
        with self._db.read() as conn:
            pending = TableRead.pending_captures(conn, now, now - MINT_LAG)
        ending = [entry for entry in pending if entry.start_raw is not None]
        for slug in {entry.slug for entry in ending} - _bells.keys():
            _bells[slug] = _freeze([entry for entry in ending if entry.slug == slug], now)
        for entry in ending:
            if now >= entry.ends_at + HOUR and (entry.slug, entry.user_id) not in _overdue:
                _overdue.add((entry.slug, entry.user_id))
                log.error(
                    "Competition %s end value for %s still missing %ds after the end",
                    entry.slug, entry.eth_address, now - entry.ends_at,
                )
        for entry in pending:
            try:
                if entry.start_raw is None:
                    self._start(entry, now)
                elif now >= entry.ends_at + END_DEADLINE:
                    self._end_at_bell(entry, now)
                elif not entry.settling and _cursor.get(entry.slug, 0) >= entry.ends_at:
                    self._end(entry, now)
            except Exception:
                log.exception("capturing %s in %s failed", entry.eth_address, entry.slug)

    def _start(self, entry: PendingCapture, now: int) -> None:
        """Cash plus positions at the board's marks, once every unsettled
        mirrored position has a book or BOOK_GRACE has passed since the run
        start."""
        wallet = self._accounts.wallet(entry.eth_address)
        at = int(time.time())
        missing = next((p.asset for p in wallet.positions if not p.settled and not feed.booked(p.asset)), None)
        if missing is not None:
            if now < entry.run_starts_at + feed.BOOK_GRACE:
                return
            log.warning(
                "Competition %s start value for %s: no book for %s, valued at its mark",
                entry.slug, entry.eth_address, missing,
            )
        self._take(entry, "START", wallet.cash_raw + _raw(sum(p.currentValue for p in wallet.positions)), at, None)

    def _end(self, entry: PendingCapture, now: int) -> None:
        """Cash plus every position sold into its token's bell state. A token
        the bell state lacks is frozen at its first end value; while its book
        is missing that value waits until BOOK_GRACE after the end, then
        takes and freezes the mark."""
        wallet = self._accounts.wallet(entry.eth_address)
        at = int(time.time())
        bell = _bells[entry.slug]
        worth = 0.0
        for position in wallet.positions:
            state = bell.get(position.asset)
            if state is None:
                state = exit_state(position)
                if state is None:
                    if now < entry.ends_at + feed.BOOK_GRACE:
                        return
                    log.warning(
                        "Competition %s end value for %s: no book for %s, valued at its mark",
                        entry.slug, entry.eth_address, position.asset,
                    )
                    state = position.curPrice
                bell[position.asset] = state
            worth += exit_value(position, state)
        self._take(entry, "END", wallet.cash_raw + _raw(worth), at, wallet.block)

    def _end_at_bell(self, entry: PendingCapture, now: int) -> None:
        with self._db.read() as conn:
            value_raw = TableRead.bell_value(conn, entry.slug, entry.user_id)
        log.warning(
            "Competition %s end value for %s taken at its bell point, %ds after the end",
            entry.slug, entry.eth_address, now - entry.ends_at,
        )
        self._take(entry, "END", value_raw, now, None)

    def _take(
        self, entry: PendingCapture, edge: Literal["START", "END"], value_raw: int, at: int, block: int | None
    ) -> None:
        with self._db.write() as conn:
            if TableWrite.capture_entry(conn, entry.slug, entry.user_id, edge, value_raw, at, block):
                log.info(
                    "Competition %s %s value for %s: %d raw, %ds past the boundary",
                    entry.slug, edge.lower(), entry.eth_address, value_raw,
                    at - (entry.ends_at if edge == "END" else entry.run_starts_at),
                )
