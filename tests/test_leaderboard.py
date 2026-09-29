from datetime import date

from agentpit.config import Settings
from agentpit.db.table_read import DailyClose, TableRead, TradeTally
from agentpit.db.table_write import TableWrite
from agentpit.services.leaderboard_service import LeaderboardService
from tests.db_helpers import fresh_test_conn, fresh_test_db


def test_only_accounts_that_traded_are_listed():
    """An account with no trade has nothing to rank, and listing every
    registered address would put people on a public board by default."""
    conn = fresh_test_conn()
    traded_id, traded_acct, traded_key = TableWrite.create_user(
        conn, email="traded@example.com", password_hash="x", handle="trader"
    )
    idle_id, _idle_acct, _idle_key = TableWrite.create_user(
        conn, email="idle@example.com", password_hash="x", handle=None
    )
    conn.execute(
        "INSERT INTO trades (TRADE_ID, TAKER_API_KEY, MATCH_TIME, STATUS) "
        "VALUES (%s, %s, %s, %s)",
        ("t1", traded_key, 1_700_000_000, "PENDING"),
    )

    rows = TableRead.list_traded_accounts(conn)
    ids = {r.user_id for r in rows}
    assert traded_id in ids
    assert idle_id not in ids
    assert traded_acct is not None
    conn.close()


def test_the_house_is_not_a_competitor():
    """It is the counterparty to nearly every trade on the platform. Ranking
    the market maker against the people trading against it is meaningless."""
    conn = fresh_test_conn()
    house_id, _acct, house_key = TableWrite.create_user(
        conn, email="house@example.com", password_hash="x", handle=None
    )
    TableWrite.mark_user_as_bot(conn, house_key)
    conn.execute(
        "INSERT INTO trades (TRADE_ID, TAKER_API_KEY, MATCH_TIME, STATUS) "
        "VALUES (%s, %s, %s, %s)",
        ("t2", house_key, 1_700_000_000, "PENDING"),
    )

    assert house_id not in {r.user_id for r in TableRead.list_traded_accounts(conn)}
    conn.close()


def test_maker_only_trade_still_counts_as_traded():
    """The membership OR must check both api-key columns. All the other
    fixtures here only ever populate TAKER_API_KEY, so on its own a query
    that quietly narrowed to taker-only would still pass the whole suite."""
    conn = fresh_test_conn()
    maker_id, _maker_acct, maker_key = TableWrite.create_user(
        conn, email="maker@example.com", password_hash="x", handle=None
    )
    _taker_id, _taker_acct, taker_key = TableWrite.create_user(
        conn, email="taker@example.com", password_hash="x", handle=None
    )
    conn.execute(
        "INSERT INTO trades (TRADE_ID, TAKER_API_KEY, MAKER_API_KEY, MATCH_TIME, STATUS) "
        "VALUES (%s, %s, %s, %s, %s)",
        ("t4", taker_key, maker_key, 1_700_000_000, "PENDING"),
    )

    ids = {r.user_id for r in TableRead.list_traded_accounts(conn)}
    assert maker_id in ids
    conn.close()


def test_snapshots_round_trip():
    conn = fresh_test_conn()
    user_id, _acct, _key = TableWrite.create_user(
        conn, email="snap@example.com", password_hash="x", handle=None
    )
    TableWrite.insert_account_snapshot(conn, user_id, 1_000, 111, 222)
    TableWrite.insert_account_snapshot(conn, user_id, 2_000, 333, 444)

    latest = TableRead.latest_account_snapshots(conn)
    assert latest[user_id] == (333, 444, 0, 0), "the most recent row wins"
    conn.close()


def test_latest_snapshot_breaks_a_tied_t_by_insertion_order():
    """A retried or duplicated pass can stamp two rows for the same account
    with the same T; SNAPSHOT_ID DESC keeps the winner deterministic -- the
    most recently written row -- instead of leaving it up to plan shape."""
    conn = fresh_test_conn()
    user_id, _acct, _key = TableWrite.create_user(
        conn, email="tie@example.com", password_hash="x", handle=None
    )
    TableWrite.insert_account_snapshot(conn, user_id, 5_000, 111, 222)
    TableWrite.insert_account_snapshot(conn, user_id, 5_000, 999, 888)

    latest = TableRead.latest_account_snapshots(conn)
    assert latest[user_id] == (999, 888, 0, 0)
    conn.close()


def test_daily_closes_keep_the_last_snapshot_of_each_utc_day():
    conn = fresh_test_conn()
    user_id, _a, _k = TableWrite.create_user(conn, email="closes@example.com", password_hash="x", handle=None)
    other_id, _b, _o = TableWrite.create_user(conn, email="other@example.com", password_hash="x", handle=None)
    x = 20_454 * 86_400
    for t, capital in ((x, 1), (x + 86_399, 2), (x + 86_405, 3), (x + 86_405, 4), (x + 3 * 86_400, 5)):
        TableWrite.insert_account_snapshot(conn, user_id, t, capital, 0)
    TableWrite.insert_account_snapshot(conn, other_id, x + 86_405, 9, 0)
    closes = TableRead.daily_closes(conn, [user_id], date(2026, 1, 2))
    conn.close()
    assert closes == {user_id: [
        DailyClose(day=date(2026, 1, 2), capital=4, deposited=0),
        DailyClose(day=date(2026, 1, 4), capital=5, deposited=0),
    ]}


def test_thinning_breaks_a_tied_close_by_insertion_order():
    conn = fresh_test_conn()
    user_id, _a, _k = TableWrite.create_user(conn, email="tieclose@example.com", password_hash="x", handle=None)
    TableWrite.insert_account_snapshot(conn, user_id, 5_000, 111, 0)
    TableWrite.insert_account_snapshot(conn, user_id, 5_000, 999, 0)
    assert TableWrite.thin_account_snapshots(conn, 10**9) == 1
    assert TableRead.latest_account_snapshots(conn)[user_id] == (999, 0, 0, 0)
    conn.close()


# ----- LeaderboardService: orchestration and the money arithmetic ----------


class _FakeOnchainBalance:
    """usd_balance keyed by address; unknown addresses read as `default`.
    `deployment_id` mirrors OnchainAdmin's property -- the valuation pass reads
    it once per account to decide whether the chain was replaced."""

    def __init__(
        self,
        balances: dict[str, int] | None = None,
        default: int = 0,
        deployment_id: str = "test-deployment",
    ):
        self._balances = balances or {}
        self._default = default
        self.deployment_id = deployment_id

    def usd_balance(self, address: str) -> int:
        return self._balances.get(address, self._default)


class _FakeAccounts:
    """`value_and_cost` keyed by address. An address with no positions -- or
    one never given a value -- reads back as (0, 0), like the real
    AccountService does for an account holding nothing.

    `costs` is optional so the tests that only care about capital stay short.
    """

    def __init__(
        self,
        values: dict[str, float] | None = None,
        costs: dict[str, float] | None = None,
    ):
        self._values = values or {}
        self._costs = costs or {}

    def value_and_cost(self, address: str) -> tuple[float, float]:
        return (self._values.get(address, 0.0), self._costs.get(address, 0.0))

    def total_value(self, address: str) -> list[dict]:
        if address not in self._values:
            return []
        return [{"user": address, "value": self._values[address]}]


def test_capital_raw_sums_cash_and_position_value():
    onchain = _FakeOnchainBalance({"0xabc": 30_000_000_000})
    accounts = _FakeAccounts({"0xabc": 70_000.0})
    service = LeaderboardService(
        db=None, onchain=onchain, accounts=accounts, settings=Settings()
    )
    assert service._capital_invested_unrealized_raw("0xabc") == (
        100_000_000_000,
        0,
        70_000_000_000,
    )


def test_capital_raw_with_no_positions_is_just_cash():
    onchain = _FakeOnchainBalance({"0xabc": 42_000_000})
    accounts = _FakeAccounts()  # no address has ever been valued
    service = LeaderboardService(
        db=None, onchain=onchain, accounts=accounts, settings=Settings()
    )
    assert service._capital_invested_unrealized_raw("0xabc") == (42_000_000, 0, 0)


def test_take_snapshot_writes_one_row_per_traded_account_with_deposited():
    conn = fresh_test_conn()
    user_id, acct, key = TableWrite.create_user(
        conn, email="valued@example.com", password_hash="x", handle=None
    )
    conn.execute(
        "INSERT INTO trades (TRADE_ID, TAKER_API_KEY, MATCH_TIME, STATUS) "
        "VALUES (%s, %s, %s, %s)",
        ("t5", key, 1_700_000_000, "PENDING"),
    )
    TableWrite.set_total_deposited(conn, user_id, 55_000_000_000)
    conn.close()

    db = fresh_test_db()
    onchain = _FakeOnchainBalance({acct.address: 30_000_000_000})
    accounts = _FakeAccounts({acct.address: 70_000.0})
    service = LeaderboardService(db, onchain, accounts, Settings())

    written = service.take_snapshot(1_700_001_000)
    assert written == 1

    check = fresh_test_conn()
    latest = TableRead.latest_account_snapshots(check)
    check.close()
    # The fake values positions at $70k with no cost recorded, so the whole
    # $70k reads as unrealized -- which is the point: none of it is banked.
    assert latest[user_id] == (
        100_000_000_000,
        55_000_000_000,
        0,
        70_000_000_000,
    )
    db.close()


def test_one_account_write_failure_does_not_cost_the_rest(monkeypatch):
    """The per-account guard must cover the write, not just the on-chain
    read. Patch insert_account_snapshot to blow up for exactly one of two
    traded accounts and confirm the other still gets its row -- and that
    take_snapshot reports 1 rather than raising out of the whole pass."""
    conn = fresh_test_conn()
    bad_id, bad_acct, bad_key = TableWrite.create_user(
        conn, email="bad@example.com", password_hash="x", handle=None
    )
    good_id, good_acct, good_key = TableWrite.create_user(
        conn, email="good@example.com", password_hash="x", handle=None
    )
    conn.execute(
        "INSERT INTO trades (TRADE_ID, TAKER_API_KEY, MATCH_TIME, STATUS) "
        "VALUES (%s, %s, %s, %s)",
        ("t-bad", bad_key, 1_700_000_000, "PENDING"),
    )
    conn.execute(
        "INSERT INTO trades (TRADE_ID, TAKER_API_KEY, MATCH_TIME, STATUS) "
        "VALUES (%s, %s, %s, %s)",
        ("t-good", good_key, 1_700_000_100, "PENDING"),
    )
    conn.close()

    real_insert = TableWrite.insert_account_snapshot

    def flaky_insert(
        db, user_id, t, capital_raw, deposited_raw, invested_raw=0, unrealized_raw=0
    ):
        if user_id == bad_id:
            raise RuntimeError("db hiccup on insert")
        return real_insert(
            db, user_id, t, capital_raw, deposited_raw, invested_raw, unrealized_raw
        )

    monkeypatch.setattr(TableWrite, "insert_account_snapshot", flaky_insert)

    db = fresh_test_db()
    onchain = _FakeOnchainBalance({bad_acct.address: 0, good_acct.address: 0})
    accounts = _FakeAccounts()
    service = LeaderboardService(db, onchain, accounts, Settings())

    written = service.take_snapshot(1_700_002_000)
    assert written == 1

    check = fresh_test_conn()
    latest = TableRead.latest_account_snapshots(check)
    check.close()
    assert good_id in latest
    assert bad_id not in latest
    db.close()


from agentpit.services.leaderboard_service import (
    SORTS,
    LeaderboardRow,
    display_name,
    rank_rows,
)


def _row(name, capital, deposited, trades=1, address="0x" + "11" * 20):
    return LeaderboardRow(
        name=name,
        address=address,
        app=None,
        capital_raw=capital,
        deposited_raw=deposited,
        trades=trades,
        first_trade_at=0,
        last_trade_at=0,
    )


def test_earned_and_return_come_off_capital_and_deposits():
    row = _row("a", capital=120_000_000_000, deposited=100_000_000_000)
    assert row.earned_raw == 20_000_000_000
    assert row.return_pct == 20.0


def test_return_is_zero_rather_than_dividing_by_zero():
    """Cannot happen once the signup grant counts as the first deposit, which
    is exactly why it does. Pinned so that stays true."""
    assert _row("a", capital=5, deposited=0).return_pct == 0.0


def test_default_sort_is_return_not_capital():
    """The default sort is what 'the leaderboard' means to a visitor, and
    capital alone ranks whoever pressed the top-up button most."""
    big_pile = _row("whale", capital=900_000_000_000, deposited=900_000_000_000)
    good_trader = _row("sharp", capital=150_000_000_000, deposited=100_000_000_000)
    assert [r.name for r in rank_rows([big_pile, good_trader], "return")] == [
        "sharp",
        "whale",
    ]
    assert [r.name for r in rank_rows([big_pile, good_trader], "capital")] == [
        "whale",
        "sharp",
    ]


def test_the_name_is_the_handle_or_the_truncated_address():
    assert display_name("degen_trader", "0x" + "ab" * 20) == "degen_trader"
    assert display_name(None, "0x1234567890abcdef1234567890abcdef12345678") == (
        "0x1234…5678"
    )


def test_ties_break_deterministically_by_address():
    """`list_traded_accounts` has no guaranteed row order of its own (a plain
    `SELECT DISTINCT`), so two accounts tied on every ranking figure could
    otherwise flip position between two cache refreshes with no change in the
    underlying data. Feeding rank_rows the same two rows in both orders must
    still produce the same output order."""
    a = _row(
        "a", capital=100_000_000_000, deposited=100_000_000_000, trades=3,
        address="0x" + "aa" * 20,
    )
    b = _row(
        "b", capital=100_000_000_000, deposited=100_000_000_000, trades=3,
        address="0x" + "bb" * 20,
    )
    for sort in SORTS:
        first = [r.address for r in rank_rows([a, b], sort)]
        second = [r.address for r in rank_rows([b, a], sort)]
        assert first == second, f"sort={sort} was not deterministic"


# ----- take_snapshot: the wipe reset moves here -----------------------------


def _seed_traded_user(email: str, *, deployed: str | None, deposited: int):
    """A traded account with a stored deployment identity and a deposit
    ledger. Returns (user_id, account)."""
    conn = fresh_test_conn()
    user_id, acct, key = TableWrite.create_user(
        conn, email=email, password_hash="x", handle=None
    )
    conn.execute(
        "INSERT INTO trades (TRADE_ID, TAKER_API_KEY, MATCH_TIME, STATUS) "
        "VALUES (%s, %s, %s, %s)",
        (f"t-{email}", key, 1_700_000_000, "PENDING"),
    )
    TableWrite.set_total_deposited(conn, user_id, deposited)
    if deployed is not None:
        TableWrite.set_deployment_id(conn, user_id, deployed)
    conn.close()
    return user_id, acct


def _deposited(user_id: str) -> int | None:
    conn = fresh_test_conn()
    row = conn.execute(
        "SELECT TOTAL_DEPOSITED FROM users WHERE USER_ID = %s", (user_id,)
    ).fetchone()
    conn.close()
    return None if row is None else row["TOTAL_DEPOSITED"]


def test_the_pass_resets_a_wiped_account_the_bots_never_top_up():
    """The whole point of moving the check here. This account authenticates by
    API key: it never logs in and never tops up, so neither of the two earlier
    homes for this reset could ever reach it."""
    user_id, acct = _seed_traded_user(
        "wiped@example.com", deployed="old-deployment", deposited=900_000_000_000
    )
    db = fresh_test_db()
    onchain = _FakeOnchainBalance({acct.address: 0}, deployment_id="new-deployment")
    service = LeaderboardService(db, onchain, _FakeAccounts(), Settings())

    service.take_snapshot(1_700_003_000)

    assert _deposited(user_id) == 0
    db.close()


def test_a_second_pass_leaves_the_reset_figure_alone():
    """The test the two previous attempts lacked. A level-triggered check --
    'balance is zero', 'identity does not match' evaluated against a value the
    reset itself does not change -- re-fires every tick and erases whatever the
    account was granted in between. The reset swaps the identity, so the second
    pass matches and changes nothing."""
    user_id, acct = _seed_traded_user(
        "twice@example.com", deployed="old-deployment", deposited=900_000_000_000
    )
    db = fresh_test_db()
    onchain = _FakeOnchainBalance({acct.address: 0}, deployment_id="new-deployment")
    service = LeaderboardService(db, onchain, _FakeAccounts(), Settings())

    service.take_snapshot(1_700_003_000)
    conn = fresh_test_conn()
    TableWrite.set_total_deposited(conn, user_id, 100_000_000_000)
    conn.close()
    service.take_snapshot(1_700_003_300)

    assert _deposited(user_id) == 100_000_000_000
    db.close()


def test_an_unchanged_deployment_accumulates_untouched():
    user_id, acct = _seed_traded_user(
        "same@example.com", deployed="same-deployment", deposited=140_000_000_000
    )
    db = fresh_test_db()
    onchain = _FakeOnchainBalance({acct.address: 0}, deployment_id="same-deployment")
    service = LeaderboardService(db, onchain, _FakeAccounts(), Settings())

    service.take_snapshot(1_700_003_000)

    assert _deposited(user_id) == 140_000_000_000
    db.close()


def test_an_absent_identity_is_recorded_without_a_reset():
    """The row predates the column, which is no evidence of a wipe. Recording
    it is what makes the NEXT redeploy detectable."""
    user_id, acct = _seed_traded_user(
        "absent@example.com", deployed=None, deposited=140_000_000_000
    )
    db = fresh_test_db()
    onchain = _FakeOnchainBalance({acct.address: 0}, deployment_id="first-seen")
    service = LeaderboardService(db, onchain, _FakeAccounts(), Settings())

    service.take_snapshot(1_700_003_000)

    assert _deposited(user_id) == 140_000_000_000
    conn = fresh_test_conn()
    stored = TableRead.get_deployment_id(conn, user_id)
    conn.close()
    assert stored == "first-seen"
    db.close()


def test_the_snapshot_records_the_reset_figure_not_the_stale_one():
    """Ordering, asserted directly: the reset must land before the deposit is
    read, or the row written this tick still carries the pre-wipe number and
    the board shows -100% until the next pass."""
    user_id, acct = _seed_traded_user(
        "ordering@example.com", deployed="old", deposited=900_000_000_000
    )
    db = fresh_test_db()
    onchain = _FakeOnchainBalance({acct.address: 0}, deployment_id="new")
    service = LeaderboardService(db, onchain, _FakeAccounts(), Settings())

    service.take_snapshot(1_700_004_000)

    conn = fresh_test_conn()
    latest = TableRead.latest_account_snapshots(conn)
    conn.close()
    assert latest[user_id] == (0, 0, 0, 0)
    db.close()


def test_thin_snapshots_leaves_the_last_48_hours_dense():
    day = 86_400
    conn = fresh_test_conn()
    user_id, _acct, _key = TableWrite.create_user(
        conn, email="dense@example.com", password_hash="x", handle=None
    )
    kept = [7 * day + 200, 8 * day + 43_201, 9 * day + 100, 9 * day + 200, 10 * day + 43_100]
    for t in (7 * day + 100, 8 * day + 43_199, *kept):
        TableWrite.insert_account_snapshot(conn, user_id, t, t, 0)
    conn.close()

    db = fresh_test_db()
    service = LeaderboardService(db, onchain=None, accounts=None, settings=Settings())
    assert service.thin_snapshots(10 * day + 43_200) == 2
    assert service.thin_snapshots(10 * day + 43_200) == 0

    check = fresh_test_conn()
    rows = check.execute("SELECT T FROM account_snapshots ORDER BY T").fetchall()
    check.close()
    assert [r["T"] for r in rows] == sorted(kept)
    db.close()


def test_a_self_matched_trade_counts_once_not_twice():
    """Both the membership EXISTS-OR and the count LATERAL's UNION ALL emit a
    hit per api-key column, so a trade whose taker and maker are the same
    account can look like two. The count must not change: this is the
    regression a plain COUNT(*) over the union would introduce silently, and
    membership must not list the same user_id twice either."""
    conn = fresh_test_conn()
    user_id, _acct, key = TableWrite.create_user(
        conn, email="selfmatch@example.com", password_hash="x", handle=None
    )
    conn.execute(
        "INSERT INTO trades (TRADE_ID, TAKER_API_KEY, MAKER_API_KEY, MATCH_TIME, STATUS) "
        "VALUES (%s, %s, %s, %s, %s)",
        ("t-self", key, key, 1_700_000_000, "PENDING"),
    )

    assert TableRead.count_trades_by_user(conn)[user_id].trades == 1
    assert [r.user_id for r in TableRead.list_traded_accounts(conn)] == [user_id]
    conn.close()


def test_counts_cover_both_sides_of_a_trade():
    conn = fresh_test_conn()
    maker_id, _m, maker_key = TableWrite.create_user(
        conn, email="counted-maker@example.com", password_hash="x", handle=None
    )
    taker_id, _t, taker_key = TableWrite.create_user(
        conn, email="counted-taker@example.com", password_hash="x", handle=None
    )
    conn.execute(
        "INSERT INTO trades (TRADE_ID, TAKER_API_KEY, MAKER_API_KEY, MATCH_TIME, STATUS) "
        "VALUES (%s, %s, %s, %s, %s)",
        ("t-both", taker_key, maker_key, 1_700_000_000, "PENDING"),
    )
    conn.execute(
        "INSERT INTO trades (TRADE_ID, TAKER_API_KEY, MATCH_TIME, STATUS) "
        "VALUES (%s, %s, %s, %s)",
        ("t-taker-only", taker_key, 1_700_000_100, "PENDING"),
    )

    tallies = TableRead.count_trades_by_user(conn)
    assert tallies[taker_id] == TradeTally(trades=2, first_trade_at=1_700_000_000, last_trade_at=1_700_000_100)
    assert tallies[maker_id] == TradeTally(trades=1, first_trade_at=1_700_000_000, last_trade_at=1_700_000_000)
    conn.close()


def test_a_failed_trade_does_not_put_an_account_on_the_board():
    """Every other trade reader in the codebase excludes STATUS = 'FAILED'
    (account_service.py, order_service.py, liquidity/tape.py's convention
    comment) because it never settled. An account whose only trade failed
    must not appear on the board, and a FAILED trade must not count toward
    an otherwise-traded account's total either."""
    conn = fresh_test_conn()
    only_failed_id, _acct, only_failed_key = TableWrite.create_user(
        conn, email="onlyfailed@example.com", password_hash="x", handle=None
    )
    mixed_id, _m, mixed_key = TableWrite.create_user(
        conn, email="mixedstatus@example.com", password_hash="x", handle=None
    )
    conn.execute(
        "INSERT INTO trades (TRADE_ID, TAKER_API_KEY, MATCH_TIME, STATUS) "
        "VALUES (%s, %s, %s, %s)",
        ("t-only-failed", only_failed_key, 1_700_000_000, "FAILED"),
    )
    conn.execute(
        "INSERT INTO trades (TRADE_ID, TAKER_API_KEY, MATCH_TIME, STATUS) "
        "VALUES (%s, %s, %s, %s)",
        ("t-mixed-good", mixed_key, 1_700_000_000, "PENDING"),
    )
    conn.execute(
        "INSERT INTO trades (TRADE_ID, TAKER_API_KEY, MATCH_TIME, STATUS) "
        "VALUES (%s, %s, %s, %s)",
        ("t-mixed-failed", mixed_key, 1_700_000_100, "FAILED"),
    )
    conn.execute(
        "INSERT INTO trades (TRADE_ID, TAKER_API_KEY, MATCH_TIME, STATUS) "
        "VALUES (%s, %s, %s, %s)",
        ("t-mixed-early-failed", mixed_key, 1_699_999_900, "FAILED"),
    )

    ids = {r.user_id for r in TableRead.list_traded_accounts(conn)}
    assert only_failed_id not in ids
    assert mixed_id in ids

    tallies = TableRead.count_trades_by_user(conn)
    assert only_failed_id not in tallies
    assert tallies[mixed_id] == TradeTally(trades=1, first_trade_at=1_700_000_000, last_trade_at=1_700_000_000)
    conn.close()


from agentpit.services.leaderboard_service import (
    compute_earned_raw,
    compute_return_pct,
)


def test_the_shared_arithmetic_matches_the_row_properties():
    """One formula, two callers: the board row and its trend. The
    properties delegate rather than restate, so a change cannot land in one
    and miss the other."""
    row = _row("a", capital=120_000_000_000, deposited=100_000_000_000)
    assert compute_earned_raw(120_000_000_000, 100_000_000_000) == row.earned_raw
    assert compute_return_pct(120_000_000_000, 100_000_000_000) == row.return_pct
    assert compute_return_pct(5, 0) == 0.0


def test_the_snapshot_records_what_the_account_put_to_work():
    """`invested` is the cost basis of the open positions, taken from the SAME
    walk as capital -- valuing an account reads every touched market on chain,
    so asking twice would double the board's most expensive operation."""
    conn = fresh_test_conn()
    user_id, acct, api_key = TableWrite.create_user(
        conn, email="invested@example.com", password_hash="x", handle=None
    )
    conn.execute(
        "INSERT INTO trades (TRADE_ID, TAKER_API_KEY, MATCH_TIME, STATUS) "
        "VALUES (%s, %s, %s, %s)",
        ("t-inv", api_key, 1_700_000_000, "PENDING"),
    )
    conn.close()

    db = fresh_test_db()
    service = LeaderboardService(
        db=db,
        onchain=_FakeOnchainBalance({acct.address: 30_000_000}),
        # Positions worth $70 that cost $50: capital is cash + value, and
        # invested is the cost -- they must not be confused for each other.
        accounts=_FakeAccounts({acct.address: 70.0}, {acct.address: 50.0}),
        settings=Settings(),
    )
    assert service.take_snapshot(1_700_001_000) == 1

    check = fresh_test_conn()
    latest = TableRead.latest_account_snapshots(check)
    check.close()
    capital, _deposited, invested, unrealized = latest[user_id]
    assert capital == 30_000_000 + 70_000_000
    assert invested == 50_000_000
    assert unrealized == 20_000_000, "the $70 they are worth less the $50 they cost"
    db.close()


def test_the_board_carries_invested_through_to_its_rows():
    conn = fresh_test_conn()
    user_id, acct, api_key = TableWrite.create_user(
        conn, email="board-inv@example.com", password_hash="x", handle="Investor"
    )
    conn.execute(
        "INSERT INTO trades (TRADE_ID, TAKER_API_KEY, MATCH_TIME, STATUS) "
        "VALUES (%s, %s, %s, %s)",
        ("t-board-inv", api_key, 1_700_000_000, "PENDING"),
    )
    TableWrite.insert_account_snapshot(
        conn, user_id, 1_700_001_000, 100_500_000, 100_000_000, 1_250_000, 200_000
    )
    conn.close()

    db = fresh_test_db()
    service = LeaderboardService(
        db=db, onchain=_FakeOnchainBalance({}), accounts=_FakeAccounts(),
        settings=Settings(),
    )
    row = next(r for r in service.build_board() if r.address == acct.address)
    assert row.invested_raw == 1_250_000
    assert row.earned_raw == 500_000
    assert row.unrealized_raw == 200_000
    # The residual: of $0.50 made, $0.20 is still riding, so $0.30 is banked.
    assert row.realized_raw == 300_000
    db.close()


def test_a_snapshot_written_before_the_column_existed_reads_as_zero_invested():
    """Rows predating INVESTED_RAW cannot be reconstructed -- the positions
    they valued have moved since -- so they read 0 rather than a guess."""
    conn = fresh_test_conn()
    user_id, _acct, _key = TableWrite.create_user(
        conn, email="legacy-inv@example.com", password_hash="x", handle=None
    )
    conn.execute(
        "INSERT INTO account_snapshots (USER_ID, T, CAPITAL_RAW, DEPOSITED_RAW) "
        "VALUES (%s, %s, %s, %s)",
        (user_id, 1_700_000_000, 10, 20),
    )
    latest = TableRead.latest_account_snapshots(conn)
    conn.close()
    assert latest[user_id] == (10, 20, 0, 0)
