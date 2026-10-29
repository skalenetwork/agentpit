import time
from datetime import UTC, date, datetime

from fastapi.testclient import TestClient

from agentpit.api.deps import get_account_service, get_onchain_admin
from agentpit.api.main import app
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.services.leaderboard_service import RANK_FLOOR, TREND_DAYS
from tests.db_helpers import fresh_test_conn


def _seed_traded_account(
    *, handle: str, capital_raw: int, deposited_raw: int, email: str
) -> str:
    """Insert a traded, snapshotted account directly against the test DB, so
    GET /leaderboard has a real row to serve -- the account_snapshots row is
    what makes it survive build_board's `latest.get(account.user_id)` check.
    Returns the account's address."""
    conn = fresh_test_conn()
    user_id, acct, key = TableWrite.create_user(
        conn, email=email, password_hash="x", handle=handle
    )
    conn.execute(
        "INSERT INTO trades (TRADE_ID, TAKER_API_KEY, MATCH_TIME, STATUS) "
        "VALUES (%s, %s, %s, %s)",
        (f"t-{handle}", key, 1_700_000_000, "PENDING"),
    )
    TableWrite.insert_account_snapshot(
        conn, user_id, 1_800_000_000, capital_raw, deposited_raw
    )
    conn.close()
    return acct.address


def test_leaderboard_is_public():
    """No key needed: it is a public board, like /positions and /value."""
    with TestClient(app) as client:
        assert client.get("/leaderboard").status_code == 200


def test_no_email_appears_in_the_payload():
    """Nobody is put on a public board under the address they signed up with.
    Asserted against the raw body so a nested field cannot slip one through.

    Seeded with a real traded, snapshotted account -- an empty board would
    make the "@" assertion trivially true regardless of whether the guarantee
    (no email field on the models; handles are [a-zA-Z0-9_]{1,15}) holds."""
    _seed_traded_account(
        handle="trader1",
        capital_raw=110_000_000_000,
        deposited_raw=100_000_000_000,
        email="trader1@example.com",
    )
    with TestClient(app) as client:
        body = client.get("/leaderboard").text
    assert "trader1" in body, "the row must actually be in the response"
    assert "@" not in body


def test_unknown_sort_falls_back_to_return():
    with TestClient(app) as client:
        assert client.get("/leaderboard?sort=nonsense").json()["sort"] == "return"


class _FakeOnchain:
    """Records nothing -- just raises, so the test can prove GET /leaderboard
    never reaches it. The chain work happens in take_snapshot, off the request."""

    def balances(self, address: str, token_ids: list[int]) -> tuple[int, int, list[int]]:
        raise AssertionError("GET /leaderboard must not call the chain at all")


class _FakeAccounts:
    """wallet also raises on purpose, alongside _FakeOnchain.balances,
    so the endpoint is proven to reach neither collaborator."""

    def wallet(self, address: str) -> None:
        raise AssertionError("GET /leaderboard must not walk positions on chain")


def test_get_leaderboard_does_not_touch_the_chain():
    """The board is served from the database; a LeaderboardService built with
    collaborators that raise on any chain read must still answer 200 with the
    seeded row -- proving both that build_board() actually ran and that it
    never called onchain or accounts."""
    address = _seed_traded_account(
        handle="chain-proof",
        capital_raw=150_000_000_000,
        deposited_raw=100_000_000_000,
        email="chain-proof@example.com",
    )
    with TestClient(app) as client:
        previous_onchain = app.dependency_overrides.get(get_onchain_admin)
        previous_accounts = app.dependency_overrides.get(get_account_service)
        app.dependency_overrides[get_onchain_admin] = lambda: _FakeOnchain()
        app.dependency_overrides[get_account_service] = lambda: _FakeAccounts()
        try:
            resp = client.get("/leaderboard")
        finally:
            if previous_onchain is None:
                app.dependency_overrides.pop(get_onchain_admin, None)
            else:
                app.dependency_overrides[get_onchain_admin] = previous_onchain
            if previous_accounts is None:
                app.dependency_overrides.pop(get_account_service, None)
            else:
                app.dependency_overrides[get_account_service] = previous_accounts

        assert resp.status_code == 200, resp.text
        entries = resp.json()["entries"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["address"] == address
        assert entry["name"] == "chain-proof"
        assert entry["capital"] == "150000000000"
        assert entry["earned"] == "50000000000"
        assert entry["trades"] == 1
        assert entry["runner"] == {"slug": "api", "label": "API", "host": None}
        assert "app" not in entry and "host" not in entry


DAY = 86_400
X = 20_454 * DAY
G = 100_000_000_000


def _account(conn, handle: str, owner: str | None = None) -> tuple[str, str]:
    user_id, _acct, key = TableWrite.create_user(
        conn, email=f"{handle}@example.com", password_hash="x", handle=handle, owner_workos_id=owner
    )
    return user_id, key


def _trade(
    conn,
    trade_id: str,
    taker: str,
    maker: str | None,
    at: int,
    status: str = "PENDING",
    *,
    price: int | None = None,
    size: int | None = None,
    kind: str | None = None,
    market: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO trades (TRADE_ID, TAKER_API_KEY, MAKER_API_KEY, MATCH_TIME, STATUS, PRICE, TRADE_SIZE, MATCH_KIND, MARKET) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (trade_id, taker, maker, at, status, price, size, kind, market),
    )


def test_stats_count_each_match_once_and_skip_tape_failures_and_bots():
    conn = fresh_test_conn()
    _, house = _account(conn, "house")
    TableWrite.mark_user_as_bot(conn, house)
    _, a = _account(conn, "alpha")
    _, b = _account(conn, "beta")
    _trade(conn, "a-house", a, house, X + DAY // 2, price=400_000, size=10_000_000)
    _trade(conn, "a-b", a, b, X + DAY + DAY // 2, price=300_000, size=5_000_000, kind="MINT")
    _trade(conn, "a-self", a, a, X + DAY + DAY // 2, price=500_000, size=2_000_000, kind="NORMAL")
    _trade(conn, "a-failed", a, house, X + DAY + DAY // 2, "FAILED", price=400_000, size=10_000_000)
    _trade(conn, "tape", "mirror-tape", "mirror-tape", X + DAY + DAY // 2, "MIRRORED", price=400_000, size=10_000_000)
    _trade(conn, "house-only", house, house, X + DAY + DAY // 2, price=400_000, size=10_000_000)
    conn.close()
    with TestClient(app) as client:
        days = client.get("/stats").json()["days"]
    assert days[0]["day"] == "2026-01-01"
    assert [(d["agents"], d["active"], d["trades"], d["volume"]) for d in days[:3]] == [
        (1, 1, 1, "4000000"),
        (2, 2, 2, "4500000"),
        (2, 0, 0, "0"),
    ]


def test_the_platform_counts_a_trade_once_where_the_board_counts_it_per_agent():
    conn = fresh_test_conn()
    _, house = _account(conn, "house")
    TableWrite.mark_user_as_bot(conn, house)
    alpha_id, a = _account(conn, "alpha")
    beta_id, b = _account(conn, "beta")
    _trade(conn, "r1", a, house, X + 100)
    _trade(conn, "r2", house, a, X + DAY + 100)
    _trade(conn, "r3", a, b, X + DAY + 200)
    _trade(conn, "r4", a, a, X + 2 * DAY + 100)
    for user_id in (alpha_id, beta_id):
        TableWrite.insert_account_snapshot(conn, user_id, X + 2 * DAY + 200, G, G)
    conn.close()
    with TestClient(app) as client:
        days = client.get("/stats").json()["days"]
        board = {
            e["name"]: (e["trades"], e["firstTradeAt"], e["lastTradeAt"])
            for e in client.get("/leaderboard").json()["entries"]
        }
    assert sum(d["trades"] for d in days) == board["alpha"][0] == 4
    assert board["alpha"][1:] == (X + 100, X + 2 * DAY + 100)
    assert board["beta"] == (1, X + DAY + 200, X + DAY + 200)


def test_the_board_trend_is_the_agents_pnl_at_each_days_close():
    conn = fresh_test_conn()
    user_id, key = _account(conn, "trendy")
    base = int(time.time()) // DAY * DAY
    _trade(conn, "trendy-1", key, None, base - 3 * DAY)
    for days_ago, earned in ((TREND_DAYS, 9), (2, 1), (2, 2), (1, 5), (0, 7)):
        TableWrite.insert_account_snapshot(conn, user_id, base - days_ago * DAY + 100 + earned, G + earned, G)
    conn.close()
    with TestClient(app) as client:
        entry = client.get("/leaderboard").json()["entries"][0]
    assert entry["trendStart"] == _day(base - 2 * DAY)
    assert entry["trend"] == ["2", "5", "7"]
    assert entry["trend"][-1] == entry["earned"]


def test_a_day_without_a_snapshot_repeats_the_close_before_it():
    conn = fresh_test_conn()
    user_id, key = _account(conn, "gappy")
    base = int(time.time()) // DAY * DAY
    _trade(conn, "gappy-1", key, None, base - 3 * DAY)
    for days_ago, earned in ((3, 4), (1, 6)):
        TableWrite.insert_account_snapshot(conn, user_id, base - days_ago * DAY + 100, G + earned, G)
    conn.close()
    with TestClient(app) as client:
        entry = client.get("/leaderboard").json()["entries"][0]
    assert entry["trendStart"] == _day(base - 3 * DAY)
    assert entry["trend"] == ["4", "4", "6", "6"]
    assert entry["trend"][-1] == entry["earned"]


def _day(t: int) -> str:
    return datetime.fromtimestamp(t, UTC).date().isoformat()


def test_rank_has_the_floor_and_moves_against_yesterdays_close():
    base = int(time.time()) // DAY * DAY
    conn = fresh_test_conn()

    def agent(handle: str, trades: int, closes: dict[int, float], *, markets: int = 10, today: int = 0) -> str:
        user_id, key = _account(conn, handle)
        for k in range(trades):
            _trade(conn, f"{handle}-{k}", key, None, base - 2 * DAY + k, market=f"m-{k % markets}")
        for k in range(today):
            _trade(conn, f"{handle}-today-{k}", key, None, base + 30 + k, market=f"m-today-{k}")
        for t, pct in closes.items():
            TableWrite.insert_account_snapshot(conn, user_id, t, G + round(pct * G / 100), G)
        return user_id

    yesterday, three_days_ago, latest = base - DAY + 3600, base - 3 * DAY + 3600, base + 60
    agent("alpha", 10, {yesterday: 10, latest: 25})
    agent("bravo", 10, {yesterday: 30, latest: 15})
    agent("charlie", 10, {three_days_ago: 20, latest: 5})
    agent("delta", RANK_FLOOR - 1, {yesterday: 1, latest: 50}, today=1)
    agent("echo", 10, {yesterday: 70, latest: 60}, markets=RANK_FLOOR - 1)
    TableWrite.delete_agent(conn, agent("foxtrot", 10, {yesterday: 40, latest: 45}), base)
    conn.close()

    with TestClient(app) as client:
        boards = [client.get(f"/leaderboard?sort={sort}").json()["entries"] for sort in ("return", "trades")]
    ranks = [{e["name"]: (e["rank"], e["rankChange"]) for e in board} for board in boards]

    assert {e["name"]: (e["trades"], e["markets"], e["tradesToday"]) for e in boards[0]} == {
        "echo": (10, RANK_FLOOR - 1, 0),
        "delta": (RANK_FLOOR, RANK_FLOOR, 1),
        "alpha": (10, 10, 0),
        "bravo": (10, 10, 0),
        "charlie": (10, 10, 0),
    }
    assert ranks[0] == {
        "echo": (None, None),
        "delta": (1, None),
        "alpha": (2, 1),
        "bravo": (3, -2),
        "charlie": (4, -2),
    }
    assert ranks[1] == ranks[0]


def test_stats_on_an_empty_platform_is_an_empty_list():
    with TestClient(app) as client:
        assert client.get("/stats").json() == {"days": []}


def _cte_scan_loops(node: dict) -> list[int]:
    own = [node["Actual Loops"]] if node["Node Type"] == "CTE Scan" else []
    return own + [n for child in node.get("Plans", []) for n in _cte_scan_loops(child)]


def test_stats_read_each_fill_once_however_many_days():
    conn = fresh_test_conn()
    _, house = _account(conn, "house")
    TableWrite.mark_user_as_bot(conn, house)
    _, a = _account(conn, "alpha")
    for d in range(5):
        _trade(conn, f"t{d}", a, house, X + d * DAY + 100)
    sent = {}

    class _Spy:
        def execute(self, sql, params):
            sent.update(sql=sql, params=params)
            return conn.execute(sql, params)

    TableRead.daily_activity(_Spy(), date(2026, 1, 10))
    plan = conn.execute(
        "EXPLAIN (ANALYZE, FORMAT JSON) " + sent["sql"], sent["params"]
    ).fetchone()
    conn.close()
    loops = _cte_scan_loops(next(iter(plan.values()))[0]["Plan"])
    assert loops and max(loops) == 1


def test_stats_are_read_once_per_window(monkeypatch):
    reads = []
    real = TableRead.daily_activity

    def counted(conn, today):
        reads.append(today)
        return real(conn, today)

    monkeypatch.setattr(TableRead, "daily_activity", staticmethod(counted))
    with TestClient(app) as client:
        first = client.get("/stats").json()
        again = client.get("/stats").json()
    assert (len(reads), again) == (1, first)


def _owners_board() -> None:
    base = int(time.time()) // DAY * DAY
    conn = fresh_test_conn()

    def agent(handle: str, owner: str | None, trades: list[int], closes: dict[int, tuple[int, int]]) -> None:
        user_id, key = _account(conn, handle, owner)
        for k, at in enumerate(trades):
            _trade(conn, f"{handle}-{k}", key, None, at, market=f"m-{k}")
        for t, (capital, deposited) in closes.items():
            TableWrite.insert_account_snapshot(conn, user_id, t, capital, deposited)

    ten = [base - 2 * DAY + k for k in range(10)]
    agent("alpha", "user_owner", ten, {base - DAY + 3600: (G + G // 20, G), base + 60: (G + G // 10, G)})
    agent("rival", None, ten, {base + 60: (G + 7 * G // 100, G)})
    agent("charlie", "user_owner", ten, {base + 60: (G + G // 20, G)})
    agent("bravo", "user_owner", [base - 5 * DAY, base - 4 * DAY], {base + 60: (3 * G + 6 * G // 10, 3 * G)})
    agent("solo", "user_solo", ten, {base + 60: (G, G)})
    agent("hid1", "user_hidden", ten, {base + 60: (G, G)})
    agent("hid2", "user_hidden", ten, {base + 60: (G, G)})
    for owner, name, shown in (("user_owner", "MapleOps", True), ("user_solo", "SoloOps", True), ("user_hidden", "HiddenOps", False)):
        TableWrite.ensure_owner(conn, owner)
        TableWrite.update_owner(conn, owner, name, shown)
    conn.close()


def test_an_owner_page_answers_any_case_sums_its_agents_and_lists_ranked_before_warming():
    _owners_board()
    base = int(time.time()) // DAY * DAY
    with TestClient(app) as client:
        resp = client.get("/owners/mapleOPS")
    assert resp.status_code == 200, resp.text
    page = resp.json()
    assert page["name"] == "MapleOps"
    assert (page["trades"], page["firstTradeAt"], page["lastTradeAt"]) == (22, base - 5 * DAY, base - 2 * DAY + 9)
    assert page["money"] == {"returnPct": 15.0, "earned": str(3 * G // 4)}
    assert [(a["name"], a["rank"]) for a in page["agents"]] == [("alpha", 1), ("charlie", 3), ("bravo", None)]


def test_an_owner_page_is_an_empty_404_unless_shown_with_two_board_agents():
    _owners_board()
    with TestClient(app) as client:
        missing = [client.get(f"/owners/{name}") for name in ("Nobody", "SoloOps", "HiddenOps", "user_owner")]
    assert [(r.status_code, r.content) for r in missing] == [(404, b"")] * 4


def test_no_owner_identity_leaks_into_public_bodies():
    _owners_board()
    with TestClient(app) as client:
        board, page = client.get("/leaderboard"), client.get("/owners/MapleOps")
    assert all("owner" not in e for e in board.json()["entries"])
    assert page.json()["name"] == "MapleOps"
    assert not any("@" in b or "user_" in b for b in (board.text, page.text))
