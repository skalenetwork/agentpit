import time
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from agentpit.api.deps import get_account_service, get_onchain_admin
from agentpit.api.main import app
from agentpit.db.table_write import TableWrite
from agentpit.services.leaderboard_service import TREND_DAYS
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
    never reaches it. The chain work happens in take_snapshot, on a timer."""

    def usd_balance(self, address: str) -> int:
        raise AssertionError("GET /leaderboard must not call the chain at all")


class _FakeAccounts:
    """total_value also raises on purpose, alongside _FakeOnchain.usd_balance,
    so the endpoint is proven to reach neither collaborator."""

    def total_value(self, address: str) -> list[dict]:
        raise AssertionError("GET /leaderboard must not walk positions on chain")


def test_get_leaderboard_does_not_touch_the_chain():
    """The board is served from the database and a cache; a LeaderboardService
    built with collaborators that raise on any chain read must still answer
    200 with the seeded row -- proving both that build_board() actually ran
    (this is a cache miss: conftest's autouse fixture clears _board_cache
    before every test, so there is nothing to hit) and that it never called
    onchain or accounts. A prior version of this test seeded no data and
    stayed silent about the cache, so it passed even while every request
    after the first was served from a stale cache entry without recomputing
    at all -- proving nothing about the request actually under test."""
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


def _account(conn, handle: str) -> tuple[str, str]:
    user_id, _acct, key = TableWrite.create_user(conn, email=f"{handle}@example.com", password_hash="x", handle=handle)
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
) -> None:
    conn.execute(
        "INSERT INTO trades (TRADE_ID, TAKER_API_KEY, MAKER_API_KEY, MATCH_TIME, STATUS, PRICE, TRADE_SIZE, MATCH_KIND) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (trade_id, taker, maker, at, status, price, size, kind),
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


def test_stats_on_an_empty_platform_is_an_empty_list():
    with TestClient(app) as client:
        assert client.get("/stats").json() == {"days": []}
