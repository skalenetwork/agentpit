import json
import logging
import random
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentpit.config import Settings
from agentpit.datastructures.competition import FINAL_DAY, HOUR, LOCK, POINT_LAG, Competition, Prize, Score, Standing, score
from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.market_state import MarketState
from agentpit.datastructures.match_leg import MICRO
from agentpit.datastructures.position_wire import PositionWire
from agentpit.db.table_read import EventRoom, HeldEvent, TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import BusinessRuleError, NotFoundError
from agentpit.liquidity import feed
from agentpit.liquidity.replica import BookSnapshot
from agentpit.services.account_service import Wallet
from agentpit.services import competition_service, leaderboard_service
from agentpit.services.balance_service import BalanceService
from agentpit.services.competition_service import (
    END_DEADLINE,
    RACE_TTL,
    CompetitionService,
    Enrollment,
    RaceBuild,
    _cursor,
    _series,
    invalidate,
)
from agentpit.services.leaderboard_service import Holdings
from tests.db_helpers import fresh_test_conn, fresh_test_db

G = 100_000_000_000
STARTS = 1_800_000_000
ENDS = STARTS + 7 * 86_400
LOCKS = STARTS - LOCK
SLUG = "october-test"
PRIZES = [Prize(usd=5, skl=1)]


class _Chain:
    """apUSD for top-ups: one balance, which a mint adds to."""

    deployment_id = "test-deployment"

    def __init__(self, cash: int):
        self._cash = cash

    def usd_balance(self, address: str) -> int:
        return self._cash

    def mint_to(self, address: str, amount_raw: int, *, timeout: int = 30) -> None:
        self._cash += amount_raw


class _Accounts:
    """Every address holds G of cash and `positions`, read at block 7."""

    def __init__(self, positions: list[PositionWire] | None = None):
        self._positions = [] if positions is None else positions

    def wallet(self, address: str) -> Wallet:
        return Wallet(7, G, self._positions)

    def total_value(self, address: str) -> list[dict]:
        return []


def _service(positions: list[PositionWire] | None = None) -> CompetitionService:
    return CompetitionService(fresh_test_db(), _Accounts(positions), Settings())


def _competition(conn, starts: int = STARTS, ends: int = ENDS, prizes: list[Prize] | None = None) -> Competition:
    competition = Competition(slug=SLUG, title="October test", starts_at=starts, ends_at=ends, prizes=prizes or [])
    TableWrite.save_competition(conn, competition, 0)
    return competition


def _agent(conn, owner: str | None = "user_owner", onboarded: bool = True) -> tuple[str, str, str]:
    user_id, acct, key = TableWrite.create_user(conn, None, None, owner_workos_id=owner)
    if onboarded:
        TableWrite.mark_user_onboarded(conn, user_id)
    return user_id, acct.address, key


def _entered(conn, user_id: str, owner: str = "user_owner") -> None:
    TableWrite.enter_competition(conn, SLUG, owner, user_id, LOCKS - 1, False)


def _entry(conn, user_id: str):
    return conn.execute("SELECT * FROM competition_entries WHERE USER_ID = %s", (user_id,)).fetchone()


def _file(folder: Path, slug: str, title: str, starts: int = STARTS, ends: int = ENDS, prizes: str = "") -> None:
    window = f"starts_at = {datetime.fromtimestamp(starts, UTC).isoformat()}\nends_at = {datetime.fromtimestamp(ends, UTC).isoformat()}"
    (folder / f"{slug}.toml").write_text(f'title = "{title}"\n{window}\n{prizes}')


def test_the_phase_turns_at_the_lock_the_start_and_the_end():
    competition = Competition(slug=SLUG, title="T", starts_at=STARTS, ends_at=ENDS, prizes=[])
    nows = (LOCKS - 1, LOCKS, STARTS - 1, STARTS, ENDS - 1, ENDS)
    assert [competition.phase(now) for now in nows] == ["upcoming", "locked", "locked", "live", "live", "ended"]


def test_with_prizes_before_the_lock_an_agent_enters_once_only_the_console_switches_and_it_can_leave():
    conn = fresh_test_conn()
    _competition(conn, prizes=PRIZES)
    first, _, _ = _agent(conn)
    second, _, _ = _agent(conn)
    service = _service()

    entered = service.enroll(first, SLUG, LOCKS - 100, switch=False)
    again = service.enroll(first, SLUG, STARTS + 50, switch=False)
    kept = _entry(conn, first)["ENROLLED_AT"]
    with pytest.raises(BusinessRuleError, match="console"):
        service.enroll(second, SLUG, LOCKS - 90, switch=False)
    service.enroll(second, SLUG, LOCKS - 80, switch=True)
    switched = (TableRead.entered_by_owner(conn, "user_owner", ENDS - 1), TableRead.entered_by_owner(conn, "user_owner", ENDS))
    service.leave(second, SLUG, LOCKS - 70)
    left = _entry(conn, second)
    service.enroll(second, SLUG, LOCKS - 60, switch=False)
    with pytest.raises(BusinessRuleError, match="final"):
        service.leave(second, SLUG, LOCKS)

    assert entered == again == Enrollment(slug=SLUG, title="October test", run_starts_at=STARTS, ends_at=ENDS)
    assert kept == LOCKS - 100
    assert switched == ({second: [SLUG]}, {})
    assert left is None


def test_from_the_lock_only_an_agent_holding_just_cash_enters_and_with_prizes_only_for_an_owner_with_no_entry():
    conn = fresh_test_conn()
    _competition(conn, prizes=PRIZES)
    entered, _, _ = _agent(conn)
    other, _, _ = _agent(conn)
    late, _, _ = _agent(conn, "user_late")
    _service().enroll(entered, SLUG, LOCKS - 1, switch=False)

    with pytest.raises(BusinessRuleError, match="after the lock"):
        _service().enroll(other, SLUG, LOCKS, switch=True)
    with pytest.raises(BusinessRuleError, match="just cash"):
        _service(positions=[PositionWire(currentValue=1.0)]).enroll(late, SLUG, STARTS + 10, switch=False)
    cash_only = [PositionWire(currentValue=0.99), PositionWire(currentValue=500.0, settled=True)]
    joined = _service(positions=cash_only).enroll(late, SLUG, STARTS + 10, switch=False)

    assert joined.run_starts_at == STARTS + 10


def test_a_practice_competition_takes_every_agent_of_an_owner_until_prizes_are_set():
    conn = fresh_test_conn()
    practice = _competition(conn)
    prized = practice.model_copy(update={"prizes": PRIZES})
    first, _, _ = _agent(conn)
    second, _, _ = _agent(conn)
    service = _service()

    service.enroll(first, SLUG, LOCKS - 100, switch=False)
    service.enroll(second, SLUG, LOCKS - 90, switch=True)
    entered = [_entry(conn, user_id)["OWNER"] for user_id in (first, second)]
    refused = TableWrite.save_competition(conn, prized, LOCKS - 80)
    service.leave(second, SLUG, LOCKS - 70)
    saved = TableWrite.save_competition(conn, prized, LOCKS - 70)
    with pytest.raises(BusinessRuleError, match="console"):
        service.enroll(second, SLUG, LOCKS - 60, switch=False)

    assert entered == ["user_owner", "user_owner"]
    assert (refused, saved) == (False, True)
    assert [c.prizes for c, _ in TableRead.competitions(conn)] == [PRIZES]


def test_only_an_onboarded_unflagged_agent_of_a_person_enters_a_competition_still_running():
    conn = fresh_test_conn()
    _competition(conn)
    pending, _, _ = _agent(conn, "user_a", onboarded=False)
    flagged, flagged_address, _ = _agent(conn, "user_b")
    TableWrite.flag_peer_transfers(conn, {flagged_address.lower(): 7})
    deleted, _, _ = _agent(conn, "user_c")
    TableWrite.delete_agent(conn, deleted, STARTS)
    bot, _, bot_key = _agent(conn, "user_d")
    TableWrite.mark_user_as_bot(conn, bot_key)
    person, _, _ = _agent(conn, None)
    able, _, _ = _agent(conn, "user_e")
    service = _service()

    for user_id in (pending, flagged, deleted, bot, person):
        with pytest.raises(BusinessRuleError, match="Only an agent"):
            service.enroll(user_id, SLUG, STARTS, switch=False)
    with pytest.raises(NotFoundError):
        service.enroll(able, "missing", STARTS, switch=False)
    with pytest.raises(BusinessRuleError, match="has ended"):
        service.enroll(able, SLUG, ENDS, switch=False)
    with pytest.raises(BusinessRuleError, match="final hour"):
        service.enroll(able, SLUG, ENDS - HOUR, switch=False)
    assert service.enroll(able, SLUG, ENDS - HOUR - 1, switch=False).run_starts_at == ENDS - HOUR - 1


def test_the_start_value_waits_for_onboarding_a_landed_top_up_a_pending_fill_and_a_book_then_holds_each_mark(
    monkeypatch, caplog
):
    conn = fresh_test_conn()
    _competition(conn)
    user_id, address, key = _agent(conn, onboarded=False)
    _entered(conn, user_id)
    TableWrite.set_last_topup_at(conn, user_id, STARTS + 100)
    monkeypatch.setattr(feed, "quote", lambda token: token)
    monkeypatch.setattr(feed, "book", lambda token: None)
    service = _service([
        PositionWire(asset="rain", size=501.0, currentValue=250.5),
        PositionWire(asset="won", size=2.0, currentValue=2.0, settled=True),
    ])

    service.capture(STARTS + 400)
    TableWrite.mark_user_onboarded(conn, user_id)
    service.capture(STARTS + 399)
    service.capture(STARTS + feed.BOOK_GRACE - 1)
    _trade(conn, key, "settling", STARTS + 500, "PENDING")
    monkeypatch.setattr(feed, "quote", lambda token: None)
    service.capture(STARTS + feed.BOOK_GRACE - 1)
    monkeypatch.setattr(feed, "quote", lambda token: token)
    waited = _entry(conn, user_id)["START_RAW"]
    read = int(time.time())
    with caplog.at_level(logging.WARNING):
        service.capture(STARTS + feed.BOOK_GRACE)
    entry = _entry(conn, user_id)

    held = json.loads(entry["START_HOLDINGS"])
    assert waited is None
    assert held == {
        "rain": {"shares": 501_000_000, "value": 250_500_000},
        "won": {"shares": 2_000_000, "value": 2_000_000},
    }
    assert (entry["START_RAW"], entry["END_BLOCK"], entry["END_HOLDINGS"]) == (G + 252_500_000, None, None)
    assert entry["START_RAW"] == G + sum(h["value"] for h in held.values())
    assert read <= entry["START_AT"] <= time.time()
    assert caplog.messages == [
        f"Competition {SLUG} start value for {address} taken with a fill still settling",
        f"Competition {SLUG} start value for {address}: no book for rain, valued at its mark",
    ]


def test_the_end_value_waits_for_the_bell_point_any_pending_fill_and_books_then_walks_every_bid(monkeypatch):
    conn = fresh_test_conn()
    _competition(conn)
    user_id, _, key = _agent(conn)
    _entered(conn, user_id)
    TableWrite.capture_entry(conn, SLUG, user_id, "START", G, STARTS, None)
    idle, _, _ = _agent(conn, "user_idle")
    _entered(conn, idle, "user_idle")
    books = {
        "deep": BookSnapshot("deep", ((400_000, 5_000_000), (5_000, 100_000_000)), ((420_000, 1_000_000),)),
        "asked": BookSnapshot("asked", (), ((900_000, 1_000_000),)),
        "empty": BookSnapshot("empty", (), ()),
    }
    monkeypatch.setattr(feed, "quote", lambda token: token if token in {*books, "gone"} else None)
    monkeypatch.setattr(feed, "book", books.get)
    held = [
        PositionWire(asset="deep", size=10.0, currentValue=4.1, curPrice=0.41),
        PositionWire(asset="asked", size=10.0, currentValue=8.0, curPrice=0.8),
        PositionWire(asset="empty", size=10.0, currentValue=3.0, curPrice=0.3),
        PositionWire(asset="local", size=10.0, currentValue=2.0, curPrice=0.2),
        PositionWire(asset="won", size=1.0, currentValue=1.0, curPrice=1.0, settled=True),
    ]
    service = _service(held)

    service.tick(ENDS + POINT_LAG - 1)
    before_the_point = _entry(conn, user_id)["END_RAW"]
    held.append(PositionWire(asset="gone", size=1.0, currentValue=0.5, curPrice=0.5))
    conn.execute(
        "INSERT INTO trades (TRADE_ID, TAKER_API_KEY, MATCH_TIME, STATUS) VALUES ('late', %s, %s, 'PENDING')",
        (key, ENDS + 1),
    )
    service.tick(ENDS + POINT_LAG)
    settling = _entry(conn, user_id)["END_RAW"]
    conn.execute("UPDATE trades SET STATUS = 'CONFIRMED'")
    service.tick(ENDS + feed.BOOK_GRACE - 1)
    unbooked = _entry(conn, user_id)["END_RAW"]
    read = int(time.time())
    service.tick(ENDS + feed.BOOK_GRACE)
    entry = _entry(conn, user_id)

    held = json.loads(entry["END_HOLDINGS"])
    assert (before_the_point, settling, unbooked) == (None, None, None)
    assert held == {
        "deep": {"shares": 10_000_000, "value": 2_025_000},
        "asked": {"shares": 10_000_000, "value": 0},
        "empty": {"shares": 10_000_000, "value": 3_000_000},
        "local": {"shares": 10_000_000, "value": 2_000_000},
        "won": {"shares": 1_000_000, "value": 1_000_000},
        "gone": {"shares": 1_000_000, "value": 500_000},
    }
    assert (entry["END_RAW"], entry["END_BLOCK"]) == (G + sum(h["value"] for h in held.values()), 7)
    assert read <= entry["END_AT"] <= time.time()
    assert _entry(conn, idle)["START_RAW"] is None


def test_every_end_value_sells_a_token_into_its_bell_state_however_the_live_book_moves(monkeypatch, caplog):
    conn = fresh_test_conn()
    _competition(conn)
    entrants = []
    for owner in ("user_a", "user_b"):
        user_id, address, _ = _agent(conn, owner)
        _entered(conn, user_id, owner)
        TableWrite.capture_entry(conn, SLUG, user_id, "START", G, STARTS, None)
        entrants.append((user_id, address))
    deep = PositionWire(asset="deep", size=10.0, currentValue=4.1, curPrice=0.41)
    leaderboard_service._holdings[entrants[0][1]] = Holdings(ENDS - 5, G, [deep], [])
    live = {"deep": BookSnapshot("deep", ((400_000, 5_000_000), (5_000, 100_000_000)), ())}
    later = [BookSnapshot("later", ((300_000, 100_000_000),), ()), BookSnapshot("later", ((800_000, 100_000_000),), ())]
    monkeypatch.setattr(feed, "quote", lambda token: token)
    monkeypatch.setattr(feed, "book", lambda token: later.pop(0) if token == "later" else live[token])
    service = _service([
        PositionWire(asset="deep", size=10.0, currentValue=10.0, curPrice=1.0, settled=True),
        PositionWire(asset="later", size=10.0, currentValue=5.0, curPrice=0.5),
    ])

    with caplog.at_level(logging.INFO, logger="agentpit.services.competition_service"):
        service.tick(ENDS)
    live["deep"] = BookSnapshot("deep", ((900_000, 100_000_000),), ())
    service.tick(ENDS + POINT_LAG)

    assert [_entry(conn, user_id)["END_RAW"] for user_id, _ in entrants] == [G + 2_025_000 + 3_000_000] * 2
    assert f"Competition {SLUG} bell state frozen for 1 tokens, 0s after the end" in caplog.messages


def test_points_are_written_once_per_due_boundary_from_the_newest_snapshot_since_the_start():
    conn = fresh_test_conn()
    _competition(conn)
    early, _, _ = _agent(conn, "user_early")
    _entered(conn, early, "user_early")
    TableWrite.capture_entry(conn, SLUG, early, "START", G, STARTS + 100, None)
    late, _, _ = _agent(conn, "user_late")
    _entered(conn, late, "user_late")
    TableWrite.capture_entry(conn, SLUG, late, "START", G + 7, STARTS + 2 * HOUR + 5, None)
    for at, capital in ((STARTS + 50, G - 1), (STARTS + 1_800, G + 1), (STARTS + HOUR, G + 2), (ENDS - 1, G + 3)):
        TableWrite.insert_account_snapshot(conn, early, at, capital, G)
    service = _service()

    def points() -> list[tuple[int, str, int]]:
        rows = conn.execute("SELECT T, USER_ID, VALUE_RAW FROM competition_points ORDER BY T, VALUE_RAW")
        return [(r["T"] - STARTS, r["USER_ID"], r["VALUE_RAW"]) for r in rows]

    service.record(STARTS + HOUR + POINT_LAG - 1)
    lagging = points()
    service.record(STARTS + HOUR + POINT_LAG)
    _cursor[SLUG] = 0
    service.record(STARTS + HOUR + POINT_LAG)
    first = points()
    service.record(STARTS + 6 * HOUR + POINT_LAG)
    caught_up = points()[1:]
    _cursor.clear()
    service.record(ENDS + POINT_LAG)
    last = points()[len(first) + len(caught_up):]

    assert lagging == []
    assert first == [(HOUR, early, G + 1)]
    assert caught_up == [
        (hour * HOUR, user_id, value)
        for hour in range(2, 7)
        for user_id, value in ((early, G + 2), (late, G + 7))
        if user_id == early or hour > 2
    ]
    assert [t for t, _, _ in last[::2]] == [ENDS - STARTS - k * HOUR for k in range(46, 0, -1)] + [ENDS - STARTS]
    assert last[-2:] == [(ENDS - STARTS, early, G + 3), (ENDS - STARTS, late, G + 7)]


def test_a_stuck_end_is_an_error_after_an_hour_and_the_bell_point_at_the_deadline_then_the_race_turns_final(
    monkeypatch, caplog
):
    conn = fresh_test_conn()
    _competition(conn)
    scanned = [7]
    monkeypatch.setattr(competition_service, "peer_next_block", lambda: scanned[0])

    def started(owner: str, deleted_at: int | None = None) -> tuple[str, str, str]:
        user_id, address, key = _agent(conn, owner)
        _entered(conn, user_id, owner)
        TableWrite.capture_entry(conn, SLUG, user_id, "START", G, STARTS, None)
        if deleted_at is not None:
            TableWrite.delete_agent(conn, user_id, deleted_at)
        return user_id, address, key

    ended, _, _ = started("user_ended")
    stuck, stuck_address, stuck_key = started("user_stuck")
    gone, _, _ = started("user_gone", ENDS - 1)
    kept, _, _ = started("user_kept", ENDS)
    unstarted, _, _ = _agent(conn, "user_unstarted")
    _entered(conn, unstarted, "user_unstarted")
    TableWrite.insert_account_snapshot(conn, stuck, ENDS - 100, G + 5, G)
    conn.execute(
        "INSERT INTO trades (TRADE_ID, TAKER_API_KEY, MATCH_TIME, STATUS) VALUES ('stuck', %s, %s, 'PENDING')",
        (stuck_key, ENDS - 1),
    )
    service = _service()

    def final() -> int | None:
        return conn.execute("SELECT FINAL_AT FROM competitions").fetchone()["FINAL_AT"]

    service.tick(ENDS + POINT_LAG)
    taken = {user_id: _entry(conn, user_id)["END_RAW"] for user_id in (ended, stuck, gone, kept, unstarted)}
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="agentpit.services.competition_service"):
        service.tick(ENDS + HOUR)
        service.tick(ENDS + HOUR + 2)
        alerted = list(caplog.messages)
        service.tick(ENDS + END_DEADLINE)
        warned = caplog.messages[len(alerted):]
    entry = _entry(conn, stuck)
    waiting = final()
    scanned[0] = 8
    service.tick(ENDS + END_DEADLINE + 2)

    assert taken == {ended: G, stuck: None, gone: None, kept: G, unstarted: None}
    bell = conn.execute("SELECT USER_ID FROM competition_points WHERE T = %s", (ENDS,)).fetchall()
    assert {r["USER_ID"] for r in bell} == {ended, stuck, kept}
    assert alerted == [f"Competition {SLUG} end value for {stuck_address} still missing {HOUR}s after the end"]
    assert warned == [
        f"Competition {SLUG} end value for {stuck_address} taken at its bell point, {END_DEADLINE}s after the end"
    ]
    assert (entry["END_RAW"], entry["END_AT"], entry["END_BLOCK"], entry["END_HOLDINGS"]) == (G + 5, ENDS + END_DEADLINE, None, None)
    assert (waiting, final()) == (None, ENDS + END_DEADLINE + 2)
    assert SLUG not in competition_service._bells


def _trade(conn, key: str, trade_id: str, at: int, status: str = "CONFIRMED") -> None:
    conn.execute(
        "INSERT INTO trades (TRADE_ID, TAKER_API_KEY, MATCH_TIME, STATUS) VALUES (%s, %s, %s, %s)",
        (trade_id, key, at, status),
    )


def _build(now: int, service: CompetitionService | None = None) -> RaceBuild:
    invalidate()
    return (service or _service()).races(now)[SLUG]


def _short(address: str) -> str:
    return f"{address[:6]}…{address[-4:]}"


def test_a_return_is_whole_basis_points_rounded_half_away_from_zero():
    half = G // 20_000

    assert [score(e, G) for e in (half, -half, half - 1, -(half - 1), 3 * half, -3 * half)] == [1, -1, 0, 0, 2, -2]
    assert (score(half, 2 * G), score(2 * half, 2 * G)) == (0, 1)
    for earned in (12_345_678, -987_654_321, 10**9 + 7, -1, 0, 3 * G):
        assert score(earned, G) / 100 == round(100 * earned / G, 2)


def test_standings_rank_live_values_then_the_bell_values_then_the_end_values():
    conn = fresh_test_conn()
    _competition(conn)
    E = 10**10

    def entrant(owner: str, value: int | None, trades: int = 1, start: int | None = G, handle: str | None = None):
        user_id, acct, key = TableWrite.create_user(conn, None, None, handle=handle, owner_workos_id=owner, agent_app="Claude")
        _entered(conn, user_id, owner)
        if start is not None:
            TableWrite.capture_entry(conn, SLUG, user_id, "START", start, STARTS + 10, None)
        if value is not None:
            TableWrite.insert_account_snapshot(conn, user_id, STARTS + 100, value, G)
        for k in range(trades):
            _trade(conn, key, f"{owner}-{k}", STARTS + 20 + k)
        return user_id, acct.address, key

    lead = entrant("user_lead", G + 2 * E)
    a = entrant("user_a", G + E)
    TableWrite.insert_account_snapshot(conn, a[0], ENDS + 5, 5 * G, G)
    c = entrant("user_c", 2 * G + E, start=2 * G)
    kept = entrant("user_kept", G + E // 10, handle="kept_bot")
    flat = entrant("user_flat", None)
    TableWrite.insert_account_snapshot(conn, flat[0], STARTS + 5, 9 * G, G)
    idle = entrant("user_idle", G + 7 * E // 10, trades=0)
    _trade(conn, idle[2], "idle-before", STARTS - 1)
    broke = entrant("user_broke", 500_000, trades=0)
    joined = entrant("user_joined", None, start=None)
    dq = entrant("user_dq", G + 3 * E)
    conn.execute("UPDATE competition_entries SET DISQUALIFIED_AT = 1 WHERE USER_ID = %s", (dq[0],))
    after = entrant("user_after", G + 4 * E)
    before = entrant("user_before", G + 35 * E // 10)
    TableWrite.flag_peer_transfers(conn, {after[1].lower(): 9, before[1].lower(): 5})
    gone = entrant("user_gone", G + 5 * E)
    TableWrite.delete_agent(conn, gone[0], STARTS + 50)

    live = _build(STARTS + 1_000)
    TableWrite.delete_agent(conn, kept[0], ENDS)
    ends = {
        lead: G, a: G + 3 * E, c: 2 * G + 2 * E // 10, kept: G + 2 * E // 10, flat: G + 5 * E // 10,
        idle: G, broke: 500_000, dq: G, after: G + E, before: G + 5 * E,
    }
    for (user_id, _, _), value in ends.items():
        TableWrite.capture_entry(conn, SLUG, user_id, "END", value, ENDS + 50, 8)
    ended = _build(ENDS + 100)
    conn.execute("UPDATE competitions SET FINAL_AT = %s", (ENDS + 150,))
    final = _build(ENDS + 200)

    def row(s: Standing) -> tuple:
        return s.address, s.rank, s.earned, s.returnPct, s.behind, s.wouldRank, s.listed, s.disqualified

    assert [row(s) for s in live.race.standings] == [
        (lead[1], 1, 2 * E, 20.0, None, None, True, False),
        (a[1], 2, E, 10.0, E, None, True, False),
        (c[1], 3, E, 5.0, E, None, True, False),
        (kept[1], 4, E // 10, 1.0, 4 * E // 10, None, True, False),
        (flat[1], 5, 0, 0.0, E // 10, None, True, False),
        (idle[1], None, 7 * E // 10, 7.0, None, 3, True, False),
        (joined[1], None, 0, 0.0, None, None, True, False),
        (broke[1], None, 500_000 - G, -100.0, None, None, False, False),
        (after[1], None, 4 * E, 40.0, None, None, False, True),
        (before[1], None, 35 * E // 10, 35.0, None, None, False, True),
        (dq[1], None, 3 * E, 30.0, None, None, True, True),
    ]
    assert (live.race.phase, live.race.final, live.race.entrants, live.race.ranked) == ("live", False, 11, 5)
    assert {(s.bids, s.bell, s.endAt) for s in live.race.standings} == {(None, None, None)}
    assert (live.entries[kept[1].lower()].standing.name, live.entries[kept[1].lower()].standing.runner.slug) == ("kept_bot", "claude")
    names = {u[1]: _short(u[1]) for u in (lead, a, c, flat, idle, broke, joined, dq, after, before)}
    assert live.race.line == f"{names[lead[1]]} leads October test at +20.00%, $10,000 ahead of {names[a[1]]}, 6d 23h left"
    assert {address: live.entries[address.lower()].line for address in (lead[1], a[1], idle[1], broke[1], joined[1], dq[1])} == {
        lead[1]: f"{names[lead[1]]} leads October test at +20.00%, $10,000 ahead of #2, 6d 23h left",
        a[1]: f"{names[a[1]]} is #2 of 5 in October test at +10.00%, $10,000 behind #1, 6d 23h left",
        idle[1]: f"{names[idle[1]]} needs one trade to place in October test; it would be #3 now",
        broke[1]: f"{names[broke[1]]} needs one trade to place in October test",
        joined[1]: f"{names[joined[1]]} has joined October test; its start value is being taken",
        dq[1]: f"{names[dq[1]]} is disqualified from October test",
    }
    assert (live.entries[a[1].lower()].above, live.entries[a[1].lower()].below) == (live.race.standings[0], live.race.standings[2])

    def scores(build: RaceBuild) -> list[tuple]:
        return [
            (s.address, s.rank, s.bell.rank if s.bell else None, s.bids.rank if s.bids else None)
            for s in build.race.standings if s.rank is not None
        ]

    assert scores(ended) == [
        (after[1], 1, 1, 2), (lead[1], 2, 2, 6), (a[1], 3, 3, 1), (c[1], 4, 4, 5), (kept[1], 5, 5, 4), (flat[1], 6, 6, 3),
    ]
    assert ended.entries[after[1].lower()].standing.bell == Score(rank=1, earned=4 * E, returnPct=40.0)
    assert ended.entries[after[1].lower()].standing.endAt == ENDS + 50
    assert ended.entries[before[1].lower()].standing.disqualified
    shown = ended.entries[kept[1].lower()].standing
    assert (shown.name, shown.listed, shown.runner.slug) == (_short(kept[1]), False, "api")
    assert ended.race.line == "October test has ended; final values at the bids are coming in"
    assert ended.entries[after[1].lower()].line == (
        f"{names[after[1]]} was #1 of 6 at the bell of October test; final values are coming in"
    )
    assert ended.entries[idle[1].lower()].line == f"{names[idle[1]]} did not place in October test"
    assert scores(final) == [
        (a[1], 1, 3, None), (after[1], 2, 1, None), (flat[1], 3, 6, None), (kept[1], 4, 5, None), (c[1], 5, 4, None),
        (lead[1], 6, 2, None),
    ]
    assert (final.race.standings[0].earned, final.race.standings[0].returnPct) == (3 * E, 30.0)
    assert final.race.line == f"{names[a[1]]} won October test with +30.00%, 6 agents placed"
    assert final.entries[a[1].lower()].line == f"{names[a[1]]} finished #1 of 6 in October test with +30.00%"


def test_the_series_ranks_each_column_like_a_reference_and_appends_what_a_rebuild_would_rank(monkeypatch):
    rng = random.Random(7)
    conn = fresh_test_conn()
    ends = STARTS + 12 * HOUR
    _competition(conn, ends=ends)
    entrants = []
    for k in range(8):
        user_id, address, key = _agent(conn, f"user_{k}")
        _entered(conn, user_id, f"user_{k}")
        start_at, start = STARTS + (rng.randrange(6 * HOUR) if k else 10), G + rng.randrange(-G // 10, G // 10)
        TableWrite.capture_entry(conn, SLUG, user_id, "START", start, start_at, None)
        snapshots = sorted((rng.randrange(STARTS, ends), G + rng.randrange(-G // 5, G // 5)) for _ in range(rng.randrange(1, 9)))
        for t, value in snapshots:
            TableWrite.insert_account_snapshot(conn, user_id, t, value, G)
        trades = sorted(rng.randrange(STARTS, ends) for _ in range(rng.randrange(4)))
        for j, t in enumerate(trades):
            _trade(conn, key, f"{k}-{j}", t)
        entrants.append((user_id, address, start, start_at, snapshots, trades))
    reads: list[int] = []
    points = TableRead.points
    monkeypatch.setattr(TableRead, "points", lambda conn, slug, after: reads.append(after) or points(conn, slug, after))
    service = _service()
    out: set[str] = set()

    def reference(t: int) -> dict[str, int]:
        keys = []
        for user_id, address, start, start_at, snapshots, trades in entrants:
            if user_id in out or start_at >= t or not trades or trades[0] >= t:
                continue
            value = next((v for at, v in reversed(snapshots) if start_at <= at < t), start)
            keys.append((score(value - start, max(start, G)), value - start, address))
        return {key[2]: place for place, key in enumerate(sorted(keys, reverse=True), 1)}

    for hour in range(1, 13):
        if hour == 7:
            leader = next(user_id for user_id, address, *_ in entrants if reference(STARTS + 6 * HOUR).get(address) == 1)
            out.add(leader)
            conn.execute("UPDATE competition_entries SET DISQUALIFIED_AT = 1 WHERE USER_ID = %s", (leader,))
        now = STARTS + hour * HOUR + POINT_LAG
        service.record(now)
        build = _build(now, service)
        at = [STARTS + h * HOUR for h in range(1, hour + 1)]
        columns = [reference(t) for t in at]
        expected = {
            address: [column.get(address) for column in columns]
            for address in {address for column in columns for address in column}
        }
        table = {s.address: s.rank for s in build.race.standings}

        assert build.race.series.at == at
        assert build.ranks == expected
        assert all(len(line.ranks) == hour + 1 and line.ranks[-1] == table[line.address] for line in build.race.series.lines)

    assert reads == [0, *(STARTS + h * HOUR for h in range(1, 6)), 0, *(STARTS + h * HOUR for h in range(7, 12))]
    _series.clear()
    assert _build(ends + POINT_LAG, service).ranks == build.ranks


def test_the_series_ranks_again_only_when_the_entries_behind_its_columns_change(monkeypatch):
    conn = fresh_test_conn()
    _competition(conn)
    keys = {}
    for owner in ("user_x", "user_y", "user_z", "user_w"):
        user_id, _, key = _agent(conn, owner)
        _entered(conn, user_id, owner)
        keys[owner] = (user_id, key)
        if owner != "user_w":
            TableWrite.capture_entry(conn, SLUG, user_id, "START", G, STARTS + 10, None)
        if owner in ("user_x", "user_y"):
            _trade(conn, key, f"{owner}-first", STARTS + 20)
    now = STARTS + 3 * HOUR + POINT_LAG
    service = _service()
    service.record(now)
    reads: list[int] = []
    points = TableRead.points
    monkeypatch.setattr(TableRead, "points", lambda conn, slug, after: reads.append(after) or points(conn, slug, after))

    changes = [
        lambda: None,
        lambda: None,
        lambda: _trade(conn, keys["user_z"][1], "z-first", now - 5),
        lambda: TableWrite.capture_entry(conn, SLUG, keys["user_w"][0], "START", G, now, None),
        lambda: conn.execute("UPDATE trades SET STATUS = 'FAILED' WHERE TRADE_ID = 'user_x-first'"),
        lambda: conn.execute("UPDATE competition_entries SET DISQUALIFIED_AT = 1 WHERE USER_ID = %s", (keys["user_y"][0],)),
        lambda: TableWrite.delete_agent(conn, keys["user_z"][0], now),
    ]
    for change in changes:
        change()
        _build(now, service)

    last = STARTS + 3 * HOUR
    assert reads == [0, last, last, last, 0, 0, 0]


def test_hours_led_lead_changes_and_the_rank_change_since_midnight_come_from_the_columns():
    conn = fresh_test_conn()
    _competition(conn)
    midnight = STARTS - STARTS % 86_400 + 86_400
    now = midnight + 2 * HOUR
    users = {}
    for owner, value, start_at in (("user_x", G + 5, STARTS + 10), ("user_y", G + 9, STARTS + 10), ("user_late", G, midnight + 10)):
        user_id, address, key = _agent(conn, owner)
        _entered(conn, user_id, owner)
        TableWrite.capture_entry(conn, SLUG, user_id, "START", G, start_at, None)
        TableWrite.insert_account_snapshot(conn, user_id, midnight + HOUR, value, G)
        _trade(conn, key, f"{owner}-first", start_at + 10)
        users[owner] = (user_id, address)
    for t, x, y in ((STARTS + HOUR, 2, 1), (STARTS + 2 * HOUR, 1, 2), (midnight, 3, 1)):
        for owner, value in (("user_x", x), ("user_y", y)):
            conn.execute(
                "INSERT INTO competition_points (SLUG, T, USER_ID, VALUE_RAW) VALUES (%s, %s, %s, %s)",
                (SLUG, t, users[owner][0], G + value),
            )

    race = _build(now).race
    shown = {s.address: (s.rank, s.rankChange, s.hoursLed) for s in race.standings}

    x, y, late = (users[owner][1] for owner in ("user_x", "user_y", "user_late"))
    assert race.series.at == [STARTS + HOUR, STARTS + 2 * HOUR, midnight]
    assert {line.address: line.ranks for line in race.series.lines} == {
        x: [1, 2, 1, 2], y: [2, 1, 2, 1], late: [None, None, None, 3],
    }
    assert shown == {x: (2, -1, 2), y: (1, 1, 1), late: (3, None, 0)}
    assert race.leadChanges == 3


def test_bids_sell_every_position_on_live_books_in_the_final_day_and_on_the_bell_state_after(monkeypatch):
    conn = fresh_test_conn()
    _competition(conn)
    users = {}
    for owner, value in (("user_x", G), ("user_y", G + 100), ("user_z", G - 50)):
        user_id, address, key = _agent(conn, owner)
        _entered(conn, user_id, owner)
        TableWrite.capture_entry(conn, SLUG, user_id, "START", G, STARTS + 10, None)
        TableWrite.insert_account_snapshot(conn, user_id, STARTS + 100, value, G)
        _trade(conn, key, f"{owner}-first", STARTS + 20)
        users[owner] = (user_id, address)
    books = {
        "deep": BookSnapshot("deep", ((400_000, 5_000_000), (5_000, 100_000_000)), ((420_000, 1_000_000),)),
        "asked": BookSnapshot("asked", (), ((900_000, 1_000_000),)),
    }
    monkeypatch.setattr(feed, "quote", lambda token: token if token in {*books, "missing"} else None)
    monkeypatch.setattr(feed, "book", books.get)
    held = [
        PositionWire(asset="deep", size=10.0, currentValue=4.1, curPrice=0.41),
        PositionWire(asset="asked", size=10.0, currentValue=8.0, curPrice=0.8),
        PositionWire(asset="local", size=10.0, currentValue=2.0, curPrice=0.2),
        PositionWire(asset="missing", size=10.0, currentValue=3.0, curPrice=0.3),
        PositionWire(asset="won", size=1.0, currentValue=1.0, curPrice=1.0, settled=True),
    ]
    x, y, z = (users[owner][1] for owner in ("user_x", "user_y", "user_z"))
    leaderboard_service._holdings[x] = Holdings(ENDS - HOUR - 5, G - 10_000_000, held, [])
    leaderboard_service._holdings[z] = Holdings(STARTS + 5, G, [], [])

    def bids(now: int) -> dict[str, Score | None]:
        return {s.address: s.bids for s in _build(now).race.standings}

    early = bids(ENDS - FINAL_DAY - 1)
    final_day = _build(ENDS - HOUR)
    TableWrite.capture_entry(conn, SLUG, users["user_y"][0], "END", G + 7, ENDS + 50, 1)
    competition_service._bells[SLUG] = {"deep": BookSnapshot("deep", ((900_000, 100_000_000),), ())}
    after = bids(ENDS + 100)
    for owner in ("user_x", "user_z"):
        TableWrite.capture_entry(conn, SLUG, users[owner][0], "END", G, ENDS + 50, 1)
    conn.execute("UPDATE competitions SET FINAL_AT = %s", (ENDS + 150,))
    final = bids(ENDS + 200)

    assert set(early.values()) == set(final.values()) == {None}
    assert {s.address: s.bids for s in final_day.race.standings} == {
        x: Score(rank=1, earned=-1_975_000, returnPct=0.0), y: None, z: None,
    }
    assert final_day.entries[x.lower()].line == (
        f"{_short(x)} is #2 of 3 in October test at 0.00%, $0 behind #1, 1h 0m left; at the bids now it would be #1"
    )
    assert after == {x: Score(rank=1, earned=5_000_000, returnPct=0.01), y: Score(rank=2, earned=7, returnPct=0.0), z: None}


def _market(conn, slug: str, end_date: int) -> str:
    market = TableWrite.create_market(
        conn,
        CreateMarketRequest(
            question=f"{slug}?",
            description="d",
            erc1155_tokens=[(f"{slug}-y", "Yes"), (f"{slug}-n", "No")],
            slug=slug,
            end_date=end_date,
            condition_id=ConditionId("0x" + slug.encode().hex().ljust(64, "0")),
            state=MarketState.ACTIVE,
        ),
        is_polygon_market=False,
    )
    return market.condition_id.value


def _position(cid: str, slug: str, outcome: int, shares: float, price: float, event: str = "") -> PositionWire:
    labels, tokens = ["Yes", "No"], [f"{slug}-y", f"{slug}-n"]
    return PositionWire(
        asset=tokens[outcome], oppositeAsset=tokens[1 - outcome], conditionId=cid, size=shares,
        currentValue=shares * price, curPrice=price, outcome=labels[outcome], outcomeIndex=outcome,
        oppositeOutcome=labels[1 - outcome], slug=slug, title=f"{slug}?", eventSlug=event,
    )


def test_fixtures_are_markets_settling_before_the_end_where_an_outcome_moves_a_place_by_event(monkeypatch):
    conn = fresh_test_conn()
    _competition(conn)
    monkeypatch.setattr(feed, "book", lambda token: None)
    users = {}
    for owner, value in (("user_x", G + 3 * 10**9), ("user_y", G + 2 * 10**9), ("user_z", G + 10**9), ("user_dq", G)):
        user_id, address, key = _agent(conn, owner)
        _entered(conn, user_id, owner)
        TableWrite.capture_entry(conn, SLUG, user_id, "START", G, STARTS + 10, None)
        TableWrite.insert_account_snapshot(conn, user_id, STARTS + 100, value, G)
        _trade(conn, key, f"{owner}-first", STARTS + 20)
        users[owner] = address
    conn.execute("UPDATE competition_entries SET DISQUALIFIED_AT = 1 WHERE OWNER = 'user_dq'")
    day = 86_400
    game, later, settled, small, first, second, other = (
        _market(conn, slug, STARTS + days * day)
        for slug, days in (("game", 2), ("later", 8), ("settled", 1), ("small", 1), ("first", 3), ("second", 4), ("other", 3.5))
    )
    market_id = conn.execute("SELECT MARKET_ID FROM markets WHERE CONDITION_ID = %s", (settled,)).fetchone()["MARKET_ID"]
    TableWrite.set_market_state(conn, market_id, MarketState.ACTIVE, MarketState.RESOLVED, (1, 0))
    for owner, positions in (
        ("user_x", [_position(game, "game", 0, 10_000, 0.5), _position(later, "later", 0, 10_000, 0.5)]),
        ("user_y", [_position(game, "game", 1, 4_000, 0.5), _position(first, "first", 0, 3_000, 0.5, "election")]),
        ("user_z", [
            _position(small, "small", 0, 1, 0.5), _position(second, "second", 0, 10_000, 0.1, "election"),
            _position(other, "other", 1, 10_000, 0.2), _position(settled, "settled", 0, 10_000, 0.5),
        ]),
        ("user_dq", [_position(game, "game", 0, 50_000, 0.5)]),
    ):
        leaderboard_service._holdings[users[owner]] = Holdings(STARTS + 100, G, positions, [])

    fixtures = _build(STARTS + 1_000).race.fixtures

    x, y, z = users["user_x"], users["user_y"], users["user_z"]
    assert [(f.market, f.endDate, f.outcomes, [(s.address, s.outcome, s.value, s.places) for s in f.stakes]) for f in fixtures] == [
        ("game", STARTS + 2 * day, ["Yes", "No"], [(x, 0, 5_000_000_000, [1, 3]), (y, 1, 2_000_000_000, [3, 1])]),
        ("first", STARTS + 3 * day, ["Yes", "No"], [(y, 0, 1_500_000_000, [1, 3])]),
        ("second", STARTS + 4 * day, ["Yes", "No"], [(z, 0, 1_000_000_000, [1, 3])]),
        ("other", STARTS + 3 * day + day // 2, ["Yes", "No"], [(z, 1, 2_000_000_000, [3, 1])]),
    ]
    assert (fixtures[0].question, fixtures[0].resolvedAt, fixtures[0].kickoff, fixtures[0].trading) == ("game?", None, None, None)


def test_the_tape_is_the_newest_run_fills_and_payouts_with_bursts_merged(monkeypatch):
    conn = fresh_test_conn()
    _competition(conn)
    cid = _market(conn, "tape", ENDS)
    market_id = conn.execute("SELECT MARKET_ID FROM markets WHERE CONDITION_ID = %s", (cid,)).fetchone()["MARKET_ID"]
    users = {}
    for owner, started in (("user_x", True), ("user_y", True), ("user_z", False)):
        user_id, address, key = _agent(conn, owner)
        _entered(conn, user_id, owner)
        if started:
            TableWrite.capture_entry(conn, SLUG, user_id, "START", G, STARTS + 10, None)
        users[owner] = (address, key)
    (x, x_key), (y, y_key), (_, z_key) = users.values()
    for trade_id, key, maker, at, side, price, size, status in (
        ("early", x_key, "house", STARTS - 10, "BUY", 400_000, 10_000_000, "CONFIRMED"),
        ("t1", x_key, "house", STARTS + 100, "BUY", 400_000, 10_000_000, "CONFIRMED"),
        ("t2", x_key, "house", STARTS + 200, "BUY", 500_000, 20_000_000, "PENDING"),
        ("t3", x_key, "house", STARTS + 300, "BUY", 600_000, 30_000_000, "CONFIRMED"),
        ("failed", x_key, "house", STARTS + 400, "BUY", 600_000, 30_000_000, "FAILED"),
        ("self", y_key, y_key, STARTS + 500, "BUY", 300_000, 5_000_000, "CONFIRMED"),
        ("unstarted", z_key, "house", STARTS + 700, "BUY", 300_000, 5_000_000, "CONFIRMED"),
        ("next", x_key, "house", STARTS + 300 + HOUR + 1, "BUY", 500_000, 1_000_000, "CONFIRMED"),
        ("late", x_key, "house", ENDS + 10, "BUY", 500_000, 1_000_000, "CONFIRMED"),
    ):
        conn.execute(
            "INSERT INTO trades (TRADE_ID, MARKET, ASSET_ID, MATCH_KIND, SIDE, PRICE, TRADE_SIZE, MATCH_TIME, STATUS, "
            "TAKER_API_KEY, MAKER_API_KEY) VALUES (%s, %s, 'tape-y', 'NORMAL', %s, %s, %s, %s, %s, %s, %s)",
            (trade_id, cid, side, price, size, at, status, key, maker),
        )
    TableWrite.set_market_state(conn, market_id, MarketState.ACTIVE, MarketState.RESOLVED, (1, 0))
    conn.execute(
        "INSERT INTO transactions (TIMESTAMP, API_KEY, TRANSACTION_TYPE, MARKET_ID, DETAILS) "
        "VALUES (to_timestamp(%s), %s, 'REDEEM', %s, %s)",
        (STARTS + 600, y_key, market_id, '{"collateral_amount": 7000000}'),
    )

    tape = _build(STARTS + 2 * HOUR).race.tape
    monkeypatch.setattr(competition_service, "TAPE", 2)
    short = _build(STARTS + 2 * HOUR).race.tape

    assert [(f.at - STARTS, f.address, f.kind, f.outcome, f.price, f.usd, f.fills) for f in tape] == [
        (300 + HOUR + 1, x, "buy", "Yes", 0.5, 500_000, 1),
        (600, y, "payout", "Yes", None, 7_000_000, 1),
        (500, y, "buy", "Yes", 0.3, 1_500_000, 1),
        (500, y, "sell", "Yes", 0.3, 1_500_000, 1),
        (300, x, "buy", "Yes", 0.533, 32_000_000, 3),
    ]
    assert {(f.market, f.question, f.name) for f in tape[:1]} == {("tape", "tape?", _short(x))}
    assert short == tape[:2]


def test_one_build_serves_every_caller_until_its_ttl_a_phase_boundary_or_an_entry(monkeypatch):
    conn = fresh_test_conn()
    _competition(conn)
    old = Competition(slug="old", title="Old", starts_at=STARTS - 30 * 86_400, ends_at=STARTS - 20 * 86_400, prizes=[])
    TableWrite.save_competition(conn, old, 0)
    conn.execute("UPDATE competitions SET FINAL_AT = 1 WHERE SLUG = 'old'")
    user_id, address, _ = _agent(conn)
    reads: list[list[str]] = []
    entries = TableRead.entries
    monkeypatch.setattr(TableRead, "entries", lambda conn, slugs: reads.append(slugs) or entries(conn, slugs))
    service = _service()

    first = service.races(LOCKS - 100)
    hit = service.races(LOCKS - 100 + RACE_TTL - 1)
    service.races(LOCKS - 100 + RACE_TTL)
    service.enroll(user_id, SLUG, LOCKS - 50, switch=False)
    entered = service.races(LOCKS - 50)[SLUG]
    service.races(LOCKS - 5)
    held = service.races(LOCKS - 1)[SLUG]
    locked = service.races(LOCKS)

    assert hit is first
    assert locked["old"] is first["old"]
    assert reads == [[SLUG, "old"], [SLUG], [SLUG], [SLUG], [SLUG]]
    assert (held.race.phase, locked[SLUG].race.phase) == ("upcoming", "locked")
    assert entered.race.line == "October test starts Jan 15 at 08:00 UTC with 1 agent entered"
    assert entered.entries[address.lower()].line == f"{_short(address)} is entered in October test, starting Jan 15 at 08:00 UTC"


def test_the_race_line_without_a_place_and_the_enroll_line():
    conn = fresh_test_conn()
    _competition(conn)
    user_id, _, _ = _agent(conn)
    _entered(conn, user_id)
    entry = Enrollment(slug=SLUG, title="October test", run_starts_at=STARTS, ends_at=ENDS)

    live = _build(ENDS - HOUR - 120).race.line
    conn.execute("UPDATE competitions SET FINAL_AT = %s", (ENDS,))
    final = _build(ENDS + 1).race.line

    assert live == "October test is live with 1h 2m left and no agent placed yet"
    assert final == "October test ended with no agent placed"
    assert (entry.line("pokeball", STARTS - 1), entry.line("pokeball", STARTS)) == (
        "pokeball is entered in October test, starting Jan 15 at 08:00 UTC",
        "pokeball joined October test; its run ends Jan 22 at 08:00 UTC",
    )


def test_an_entry_pauses_top_ups_from_its_run_start_until_its_end_value_or_until_its_end_without_a_start():
    now = int(time.time())
    conn = fresh_test_conn()
    ends = now + 3_600
    _competition(conn, starts=now + 60, ends=ends)
    user_id, _, _ = _agent(conn)
    TableWrite.enter_competition(conn, SLUG, "user_owner", user_id, now - LOCK, False)
    balance = BalanceService(fresh_test_db(), _Chain(G // 2), Settings(), _Accounts())
    user = TableRead.get_user_by_userid(conn, user_id)
    assert user is not None

    locked = BalanceService(fresh_test_db(), _Chain(G // 2), Settings(), _Accounts()).top_up(user, now)
    TableWrite.set_last_topup_at(conn, user_id, None)
    conn.execute("UPDATE competitions SET STARTS_AT = %s", (now,))
    paused = balance.top_up(user, now)
    status = balance.next_allowed(user)
    claimed = TableWrite.claim_topup(conn, user_id, now, now, 1, G)
    conn.execute("UPDATE competitions SET STARTS_AT = %s, ENDS_AT = %s", (now - 60, now))
    unstarted = (balance.next_allowed(user), TableRead.topup_paused_until(conn, user_id, now))
    TableWrite.capture_entry(conn, SLUG, user_id, "START", G // 2, now - 60, None)
    started = TableRead.topup_paused_until(conn, user_id, now)
    TableWrite.capture_entry(conn, SLUG, user_id, "END", G // 2, now, 1)
    resumed = balance.top_up(user, now)

    assert locked.minted_raw == G // 2
    assert (paused.minted_raw, paused.next_allowed_at, status, claimed) == (0, ends, ends, False)
    assert (unstarted, started) == ((0, None), now)
    assert resumed.minted_raw == G // 2


def _event(conn, title: str, slugs: list[str]) -> None:
    event_id = conn.execute(
        "INSERT INTO events (SLUG, TITLE) VALUES (%s, %s) RETURNING EVENT_ID", (slugs[0], title)
    ).fetchone()["EVENT_ID"]
    conn.execute("UPDATE markets SET EVENT_ID = %s WHERE SLUG = ANY(%s)", (event_id, slugs))


def _fill(conn, key: str, trade_id: str, cid: str, token: str, side: str, shares: int, status: str = "CONFIRMED") -> None:
    conn.execute(
        "INSERT INTO trades (TRADE_ID, MARKET, ASSET_ID, MATCH_KIND, SIDE, TRADE_SIZE, MATCH_TIME, STATUS, TAKER_API_KEY) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (trade_id, cid, token, "MINT" if side == "BUY" else "NORMAL", side, shares * MICRO, STARTS - 10, status, key),
    )


def _market_id(conn, slug: str) -> int:
    return conn.execute("SELECT MARKET_ID FROM markets WHERE SLUG = %s", (slug,)).fetchone()["MARKET_ID"]


def test_a_prize_entrant_in_its_run_holds_an_events_shares_over_its_unresolved_markets_from_fills_and_splits():
    conn = fresh_test_conn()
    _competition(conn, prizes=PRIZES)
    user_id, _, key = _agent(conn)
    _, _, stranger = _agent(conn, "user_stranger")
    _entered(conn, user_id)
    win, draw, lose, other = (_market(conn, slug, ENDS) for slug in ("win", "draw", "lose", "other"))
    _event(conn, "Venezia vs Lecce", ["win", "draw", "lose"])
    for trade_id, cid, token, side, shares, status in (
        ("bought", win, "win-n", "BUY", 9_000, "CONFIRMED"),
        ("sold", win, "win-n", "SELL", 1_000, "PENDING"),
        ("short", win, "win-y", "SELL", 700, "CONFIRMED"),
        ("draw", draw, "draw-y", "BUY", 3_000, "CONFIRMED"),
        ("failed", draw, "draw-y", "BUY", 50_000, "FAILED"),
        ("lost", lose, "lose-y", "BUY", 30_000, "CONFIRMED"),
        ("alone", other, "other-y", "BUY", 25_000, "CONFIRMED"),
    ):
        _fill(conn, key, trade_id, cid, token, side, shares, status)
    TableWrite.log_transaction(conn, key, "SPLIT", _market_id(conn, "draw"), {"amount": 2_000 * MICRO})
    TableWrite.log_transaction(conn, key, "MERGE", _market_id(conn, "draw"), {"amount": 500 * MICRO})
    TableWrite.set_market_state(conn, _market_id(conn, "lose"), MarketState.ACTIVE, MarketState.RESOLVED, (1, 0))
    room = TableRead.event_room(conn, key, _market_id(conn, "lose"), STARTS)
    alone = TableRead.event_room(conn, key, _market_id(conn, "other"), ENDS - 1)
    outside = [
        TableRead.event_room(conn, key, _market_id(conn, "win"), STARTS - 1),
        TableRead.event_room(conn, key, _market_id(conn, "win"), ENDS),
        TableRead.event_room(conn, stranger, _market_id(conn, "win"), STARTS),
    ]
    conn.execute("UPDATE competition_entries SET ENROLLED_AT = %s", (STARTS + 100,))
    late = [TableRead.event_room(conn, key, _market_id(conn, "win"), now) for now in (STARTS + 99, STARTS + 100)]
    conn.execute("UPDATE competitions SET PRIZES = '[]'")
    practice = TableRead.event_room(conn, key, _market_id(conn, "win"), STARTS + 100)

    assert room == EventRoom(title="October test", event="Venezia vs Lecce", held=14_000 * MICRO, room=6_000 * MICRO)
    assert alone == EventRoom(title="October test", event="other?", held=25_000 * MICRO, room=0)
    assert TableRead.over_cap(conn, key) == HeldEvent(event="other?", held=25_000 * MICRO)
    assert outside == [None, None, None]
    assert late == [None, room]
    assert practice is None


def test_a_prize_start_waits_past_the_book_grace_while_an_event_holds_over_the_cap_and_starts_once_sold_down():
    conn = fresh_test_conn()
    _competition(conn, prizes=PRIZES)
    practice = Competition(slug="practice", title="Practice", starts_at=STARTS, ends_at=ENDS, prizes=[])
    TableWrite.save_competition(conn, practice, 0)
    user_id, address, key = _agent(conn)
    _entered(conn, user_id)
    other_id, _, other_key = _agent(conn, "user_other")
    TableWrite.enter_competition(conn, "practice", "user_other", other_id, LOCKS - 1, False)
    cid = _market(conn, "win", ENDS)
    _event(conn, "Venezia vs Lecce", ["win"])
    for k in (key, other_key):
        _fill(conn, k, f"{k}-bought", cid, "win-n", "BUY", 25_000)
    service = _service()

    service.capture(STARTS + feed.BOOK_GRACE)
    held = (_entry(conn, user_id)["START_RAW"], _entry(conn, other_id)["START_RAW"])
    line = _build(STARTS + feed.BOOK_GRACE).entries[address.lower()].line
    _fill(conn, key, "sold", cid, "win-n", "SELL", 5_000)
    service.capture(STARTS + feed.BOOK_GRACE + 1)

    assert held == (None, G)
    assert line == (
        f"{_short(address)} has joined October test; its start waits until its 25,000 shares in Venezia vs Lecce "
        "are at most 20,000"
    )
    assert _entry(conn, user_id)["START_RAW"] == G


def test_the_sync_keeps_what_is_fixed_and_deletes_only_unstarted_competitions_with_no_file_or_entry(tmp_path, caplog):
    conn = fresh_test_conn()
    old = Competition(slug="x", title="Old", starts_at=STARTS, ends_at=ENDS, prizes=[])
    locked = old.model_copy(update={"starts_at": STARTS + 1})
    upcoming = old.model_copy(update={"starts_at": STARTS + LOCK + 1})
    for slug, competition in (
        ("bad", old), ("moved", old), ("renamed", old), ("gone", old),
        ("later", locked), ("reprized", locked), ("entered", locked), ("dropped", locked), ("early", upcoming),
    ):
        TableWrite.save_competition(conn, competition.model_copy(update={"slug": slug}), 0)
    conn.execute("UPDATE competitions SET FINAL_AT = 1 WHERE SLUG = 'renamed'")
    TableWrite.enter_competition(conn, "entered", "user_owner", "user_id", 0, False)
    prizes = "[prizes]\n1-2 = { usd = 5, skl = 1 }\n"
    _file(tmp_path, "new", "New", prizes=prizes)
    _file(tmp_path, "moved", "New", ends=ENDS + 1)
    _file(tmp_path, "later", "New", ends=ENDS + 1)
    _file(tmp_path, "renamed", "New")
    _file(tmp_path, "reprized", "New", starts=STARTS + 1, prizes=prizes)
    _file(tmp_path, "early", "New", starts=STARTS + LOCK + 1, prizes=prizes)
    _file(tmp_path, "bad", "New", prizes="[prizes]\n")
    (tmp_path / "notes.md").write_text("not a competition")

    with caplog.at_level(logging.ERROR):
        CompetitionService.sync(conn, tmp_path, STARTS)

    paid = [Prize(usd=5, skl=1)] * 2
    assert {c.slug: c for c, _ in TableRead.competitions(conn)} == {
        "new": Competition(slug="new", title="New", starts_at=STARTS, ends_at=ENDS, prizes=paid),
        "renamed": old.model_copy(update={"slug": "renamed", "title": "New"}),
        "moved": old.model_copy(update={"slug": "moved"}),
        "later": Competition(slug="later", title="New", starts_at=STARTS, ends_at=ENDS + 1, prizes=[]),
        "bad": old.model_copy(update={"slug": "bad"}),
        "reprized": locked.model_copy(update={"slug": "reprized"}),
        "early": upcoming.model_copy(update={"slug": "early", "title": "New", "prizes": paid}),
        "gone": old.model_copy(update={"slug": "gone"}),
        "entered": locked.model_copy(update={"slug": "entered"}),
    }
    assert [m.split(" ")[2] for m in caplog.messages[:3]] == ["bad.toml", "moved.toml", "reprized.toml"]
    assert [m.split(" ")[1] for m in caplog.messages[3:]] == ["entered", "gone"]
    assert conn.execute("SELECT FINAL_AT FROM competitions WHERE SLUG = 'renamed'").fetchone()["FINAL_AT"] == 1


def test_the_sync_changes_nothing_without_its_folder(tmp_path):
    conn = fresh_test_conn()
    _competition(conn)

    CompetitionService.sync(conn, tmp_path / "competitions", STARTS)

    assert [c.slug for c, _ in TableRead.competitions(conn)] == [SLUG]
