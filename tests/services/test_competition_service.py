import time

import pytest

from agentpit.config import Settings
from agentpit.datastructures.competition import LOCK, Competition, Prize
from agentpit.datastructures.position_wire import PositionWire
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import BusinessRuleError, NotFoundError
from agentpit.liquidity import feed
from agentpit.liquidity.replica import BookSnapshot
from agentpit.services.balance_service import BalanceService
from agentpit.services.competition_service import CompetitionService, Enrollment
from agentpit.services.leaderboard_service import RANK_FLOOR
from tests.db_helpers import fresh_test_conn, fresh_test_db

G = 100_000_000_000
STARTS = 1_800_000_000
ENDS = STARTS + 7 * 86_400
LOCKS = STARTS - LOCK
SLUG = "october-test"


class _Chain:
    """usd_balance answers each read in turn, repeating the last; mints add to it."""

    deployment_id = "test-deployment"

    def __init__(self, *reads: int):
        self._reads = list(reads)

    def usd_balance(self, address: str) -> int:
        return self._reads.pop(0) if len(self._reads) > 1 else self._reads[0]

    def mint_to(self, address: str, amount_raw: int, *, timeout: int = 30) -> None:
        self._reads = [self._reads[-1] + amount_raw]


class _Positions:
    def __init__(self, positions: list[PositionWire] | None = None):
        self._positions = positions or []

    def list_positions(self, address: str) -> list[PositionWire]:
        return self._positions

    def total_value(self, address: str) -> list[dict]:
        return []


def _service(chain: _Chain | None = None, positions: list[PositionWire] | None = None) -> CompetitionService:
    return CompetitionService(fresh_test_db(), chain or _Chain(G), _Positions(positions), Settings())


def _competition(conn, starts: int = STARTS, ends: int = ENDS) -> None:
    competition = Competition(slug=SLUG, title="October test", starts_at=starts, ends_at=ends, prize_token="SKL", prizes=[Prize()])
    TableWrite.save_competition(conn, competition, 0)


def _agent(conn, owner: str | None = "user_owner", onboarded: bool = True) -> tuple[str, str, str]:
    user_id, acct, key = TableWrite.create_user(conn, None, None, owner_workos_id=owner)
    if onboarded:
        TableWrite.mark_user_onboarded(conn, user_id)
    return user_id, acct.address, key


def _entered(conn, user_id: str, owner: str = "user_owner") -> None:
    TableWrite.enter_competition(conn, SLUG, owner, user_id, LOCKS - 1, False)


def _entry(conn, user_id: str):
    return conn.execute("SELECT * FROM competition_entries WHERE USER_ID = %s", (user_id,)).fetchone()


def test_the_phase_turns_at_the_lock_the_start_and_the_end():
    competition = Competition(slug=SLUG, title="T", starts_at=STARTS, ends_at=ENDS, prize_token="SKL", prizes=[Prize()])
    nows = (LOCKS - 1, LOCKS, STARTS - 1, STARTS, ENDS - 1, ENDS)
    assert [competition.phase(now) for now in nows] == ["upcoming", "locked", "locked", "live", "live", "ended"]


def test_before_the_lock_an_agent_enters_once_only_the_console_switches_and_it_can_leave():
    conn = fresh_test_conn()
    _competition(conn)
    first, _, _ = _agent(conn)
    second, _, _ = _agent(conn)
    service = _service()

    entered = service.enroll(first, SLUG, LOCKS - 100, switch=False)
    again = service.enroll(first, SLUG, STARTS + 50, switch=False)
    kept = _entry(conn, first)["ENROLLED_AT"]
    with pytest.raises(BusinessRuleError, match="console"):
        service.enroll(second, SLUG, LOCKS - 90, switch=False)
    service.enroll(second, SLUG, LOCKS - 80, switch=True)
    switched = (service.entered("user_owner", ENDS - 1), service.entered("user_owner", ENDS))
    service.leave(second, SLUG, LOCKS - 70)
    left = _entry(conn, second)
    service.enroll(second, SLUG, LOCKS - 60, switch=False)
    with pytest.raises(BusinessRuleError, match="final"):
        service.leave(second, SLUG, LOCKS)

    assert entered == again == Enrollment(slug=SLUG, run_starts_at=STARTS, ends_at=ENDS)
    assert kept == LOCKS - 100
    assert switched == ({second: [SLUG]}, {})
    assert left is None


def test_from_the_lock_only_an_owner_with_no_entry_enters_an_agent_holding_just_cash():
    conn = fresh_test_conn()
    _competition(conn)
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


def test_the_start_value_waits_for_onboarding_a_landed_top_up_and_steady_cash():
    conn = fresh_test_conn()
    _competition(conn)
    user_id, _, _ = _agent(conn, onboarded=False)
    _entered(conn, user_id)
    TableWrite.set_last_topup_at(conn, user_id, STARTS - 100)
    held = [PositionWire(currentValue=250.5)]

    _service(positions=held).capture(STARTS)
    TableWrite.mark_user_onboarded(conn, user_id)
    _service(positions=held).capture(STARTS + 199)
    _service(_Chain(G, G + 1), held).capture(STARTS + 200)
    waited = _entry(conn, user_id)["START_RAW"]
    _service(positions=held).capture(STARTS + 200)

    assert waited is None
    assert _entry(conn, user_id)["START_RAW"] == G + 250_500_000


def test_the_end_value_walks_every_bid_and_waits_for_pending_fills_and_books(monkeypatch):
    conn = fresh_test_conn()
    _competition(conn)
    user_id, _, key = _agent(conn)
    _entered(conn, user_id)
    TableWrite.capture_entry(conn, SLUG, user_id, "START", G)
    books = {
        "deep": BookSnapshot("deep", ((400_000, 5_000_000), (5_000, 100_000_000)), ((420_000, 1_000_000),)),
        "asked": BookSnapshot("asked", (), ((900_000, 1_000_000),)),
        "empty": BookSnapshot("empty", (), ()),
    }
    monkeypatch.setattr(feed, "quote", lambda token: token if token in {*books, "gone"} else None)
    monkeypatch.setattr(feed, "book", books.get)
    held = [
        PositionWire(asset="deep", size=10.0, currentValue=4.1),
        PositionWire(asset="asked", size=10.0, currentValue=8.0),
        PositionWire(asset="empty", size=10.0, currentValue=3.0),
        PositionWire(asset="local", size=10.0, currentValue=2.0),
        PositionWire(asset="deep", size=1.0, currentValue=1.0, settled=True),
        PositionWire(asset="gone", size=1.0, currentValue=0.5),
    ]
    conn.execute(
        "INSERT INTO trades (TRADE_ID, MAKER_API_KEY, MATCH_TIME, STATUS) VALUES ('late', %s, %s, 'PENDING')",
        (key, ENDS - 1),
    )

    _service(positions=held).capture(ENDS + 600)
    conn.execute("UPDATE trades SET STATUS = 'CONFIRMED'")
    _service(positions=held).capture(ENDS + 599)
    waited = _entry(conn, user_id)["END_RAW"]
    _service(positions=held).capture(ENDS + 600)

    assert waited is None
    assert _entry(conn, user_id)["END_RAW"] == G + 2_025_000 + 3_000_000 + 2_000_000 + 1_000_000 + 500_000


def test_standings_rank_by_return_from_the_floor_and_turn_final_with_every_end_value():
    conn = fresh_test_conn()
    _competition(conn)

    def entrant(owner: str, pct: int, trades: int, before: int = 0, start: int | None = G) -> tuple[str, str]:
        user_id, address, key = _agent(conn, owner)
        _entered(conn, user_id, owner)
        if start is not None:
            TableWrite.capture_entry(conn, SLUG, user_id, "START", start)
        TableWrite.insert_account_snapshot(conn, user_id, STARTS + 100, G + G * pct // 100, G)
        times = [STARTS + k for k in range(trades)] + [STARTS - 1 - k for k in range(before)]
        for k, at in enumerate(times):
            conn.execute(
                "INSERT INTO trades (TRADE_ID, TAKER_API_KEY, MATCH_TIME, STATUS) VALUES (%s, %s, %s, 'CONFIRMED')",
                (f"{owner}-{k}", key, at),
            )
        return user_id, address

    a = entrant("user_a", 10, RANK_FLOOR)
    b = entrant("user_b", 20, RANK_FLOOR - 1, before=5)
    c = entrant("user_c", 5, RANK_FLOOR)
    d = entrant("user_d", 30, RANK_FLOOR)
    conn.execute("UPDATE competition_entries SET DISQUALIFIED_AT = 1 WHERE USER_ID = %s", (d[0],))
    e = entrant("user_e", 0, 0, start=None)
    g = entrant("user_g", 40, RANK_FLOOR)
    TableWrite.flag_peer_transfers(conn, {g[1].lower(): 9})
    gone = entrant("user_f", 50, RANK_FLOOR)
    TableWrite.delete_agent(conn, gone[0], STARTS)
    service = _service()

    live = service.page(SLUG, STARTS + 1000)
    TableWrite.capture_entry(conn, SLUG, a[0], "END", G + G // 2)
    for user_id, _ in (b, c, d):
        TableWrite.capture_entry(conn, SLUG, user_id, "END", G)
    waiting = service.page(SLUG, ENDS)
    TableWrite.capture_entry(conn, SLUG, g[0], "END", G)
    final = service.page(SLUG, ENDS)

    shown = [(s.address, s.rank, s.trades, s.listed, s.disqualified) for s in live.standings]
    assert shown == [
        (a[1], 1, RANK_FLOOR, True, False),
        (c[1], 2, RANK_FLOOR, True, False),
        (b[1], None, RANK_FLOOR - 1, True, False),
        (e[1], None, 0, False, False),
        (g[1], None, RANK_FLOOR, False, True),
        (d[1], None, RANK_FLOOR, True, True),
    ]
    assert (live.phase, live.entrants, live.final) == ("live", 6, False)
    assert (live.standings[0].earned_raw, live.standings[0].return_pct) == (G // 10, 10.0)
    assert (waiting.phase, waiting.final, final.final) == ("ended", False, True)
    assert (final.standings[0].address, final.standings[0].return_pct) == (a[1], 50.0)


def test_an_entry_pauses_top_ups_from_its_lock_until_its_end_value():
    now = int(time.time())
    conn = fresh_test_conn()
    ends = now + LOCK + 3_600
    _competition(conn, starts=now + LOCK + 60, ends=ends)
    user_id, _, _ = _agent(conn)
    _entered(conn, user_id)
    balance = BalanceService(fresh_test_db(), _Chain(G // 2), Settings(), _Positions())
    user = TableRead.get_user_by_userid(conn, user_id)
    assert user is not None

    before = balance.next_allowed(user)
    conn.execute("UPDATE competitions SET STARTS_AT = %s", (now + 60,))
    paused = balance.top_up(user, now)
    status = balance.next_allowed(user)
    claimed = TableWrite.claim_topup(conn, user_id, now, now, 1, G)
    TableWrite.capture_entry(conn, SLUG, user_id, "START", G // 2)
    TableWrite.capture_entry(conn, SLUG, user_id, "END", G // 2)
    resumed = balance.top_up(user, now)

    assert before == 0
    assert (paused.minted_raw, paused.next_allowed_at, status, claimed) == (0, ends, ends, False)
    assert resumed.minted_raw == G // 2
