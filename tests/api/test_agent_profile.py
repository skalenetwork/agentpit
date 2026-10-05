import time
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from agentpit.api.deps import get_onchain_admin
from agentpit.api.main import app
from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.market_state import MarketState
from agentpit.datastructures.position_wire import PositionWire
from agentpit.db.table_write import TableWrite
from agentpit.services.leaderboard_service import Holdings, _holdings
from tests.db_helpers import fresh_test_conn

G = 100_000_000_000
NOW = int(time.time())
CRYPTO = "0x" + "ab" * 32
UNFILED = "0x" + "cd" * 32


class _NoChain:
    def usd_balance(self, address: str) -> int:
        raise AssertionError("a profile must not read the chain once the pass has valued the account")

    def ctf_balances(self, address: str, token_ids: list[int]) -> list[int]:
        raise AssertionError("a profile must not read the chain once the pass has valued the account")


@pytest.fixture(autouse=True)
def _no_chain():
    previous = app.dependency_overrides[get_onchain_admin]
    app.dependency_overrides[get_onchain_admin] = lambda: _NoChain()
    yield
    app.dependency_overrides[get_onchain_admin] = previous


def _market(conn, polymarket_id: int | None = None) -> None:
    event = TableWrite.upsert_event(conn, slug="btc", title="Bitcoin", category="Crypto")
    TableWrite.create_market(
        conn,
        CreateMarketRequest(
            question="Will Bitcoin dip?",
            description="d",
            erc1155_tokens=[("btc-y", "Yes"), ("btc-n", "No")],
            slug="btc",
            condition_id=ConditionId(CRYPTO),
            state=MarketState.ACTIVE,
            event_id=event.event_id,
            polymarket_id=polymarket_id,
        ),
        is_polygon_market=False,
    )


def _agent(conn, handle: str, return_pct: float, trades: int, *, invested: int = 0, unrealized: int = 0) -> str:
    """A snapshotted agent with `trades` fills of 4 Yes at 25¢, the newest at `NOW - 60`,
    whose holdings the pass has already kept."""
    user_id, acct, key = TableWrite.create_user(conn, email=f"{handle}@example.com", password_hash="x", handle=handle)
    for k in range(trades):
        conn.execute(
            "INSERT INTO trades (TRADE_ID, MARKET, ASSET_ID, SIDE, PRICE, TRADE_SIZE, MATCH_KIND, STATUS, "
            "TAKER_API_KEY, MATCH_TIME) VALUES (%s, %s, 'btc-y', 'BUY', 250000, 4000000, 'NORMAL', 'CONFIRMED', %s, %s)",
            (uuid.uuid4().hex, CRYPTO, key, NOW - 60 - k),
        )
    TableWrite.insert_account_snapshot(
        conn, user_id, NOW - 30, G + round(return_pct * G / 100), G, invested, unrealized
    )
    _holdings[acct.address] = Holdings(valued_at=NOW - 30, cash_raw=0, positions=[], closed=[])
    return acct.address


def _profile(client: TestClient, address: str) -> dict:
    resp = client.get(f"/agents/{address}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_a_lowercase_address_answers_with_the_checksummed_one():
    conn = fresh_test_conn()
    _market(conn)
    address = _agent(conn, "Solo", 0.0, 1)
    conn.close()
    with TestClient(app) as client:
        body = _profile(client, address.lower())
    assert body["address"] == address
    assert (body["name"], body["trades"], body["valuedAt"]) == ("Solo", 1, NOW - 30)
    assert body["runner"] == {"slug": "api", "label": "API", "host": None}
    assert body["positions"] == {"count": 0, "mark": "0", "sellsFor": "0", "top": [], "open": []}
    assert body["book"] is None and body["record"] is None
    assert body["activity"] == [
        {
            "at": NOW - 60,
            "type": "TRADE",
            "side": "BUY",
            "outcome": "Yes",
            "shares": 4.0,
            "dollars": "1000000",
            "title": "Will Bitcoin dip?",
            "icon": None,
            "category": "Crypto",
            "url": None,
            "eventTitle": None,
            "endDate": None,
            "resolvedAt": None,
            "winner": None,
        }
    ]


def test_a_polymarket_fill_links_to_its_market():
    conn = fresh_test_conn()
    _market(conn, polymarket_id=7)
    address = _agent(conn, "Linked", 0.0, 1)
    conn.close()
    with TestClient(app) as client:
        body = _profile(client, address)
    assert body["activity"][0]["url"] == "https://polymarket.com/market/btc"


def test_each_market_carries_its_event_and_status():
    conn = fresh_test_conn()
    _market(conn)
    fed = TableWrite.upsert_event(conn, slug="fed", title="Fed decision in October?", category="Business")
    cut, hold = (
        TableWrite.create_market(
            conn,
            CreateMarketRequest(
                question=question,
                description="d",
                erc1155_tokens=[(f"{slug}-y", "Yes"), (f"{slug}-n", "No")],
                slug=slug,
                condition_id=ConditionId(condition_id),
                state=MarketState.ACTIVE,
                event_id=fed.event_id,
                start_date=NOW - 10 * 86_400,
                end_date=end,
            ),
            is_polygon_market=False,
        )
        for question, slug, condition_id, end in (
            ("Fed cuts 25 bps?", "cut", UNFILED, NOW - 86_400),
            ("Fed holds?", "hold", "0x" + "ef" * 32, NOW + 86_400),
        )
    )
    TableWrite.resolve_market(conn, cut.market_id, 1)
    address = _agent(conn, "Contexts", 0.0, 1)
    conn.close()
    with TestClient(app) as client:
        _holdings[address] = Holdings(
            valued_at=NOW,
            cash_raw=0,
            positions=[
                PositionWire(conditionId=c, title=t, currentValue=v)
                for c, t, v in ((CRYPTO, "Will Bitcoin dip?", 3.0), (cut.condition_id.value, "Fed cuts 25 bps?", 2.0), (hold.condition_id.value, "Fed holds?", 1.0))
            ],
            closed=[],
        )
        body = _profile(client, address)
    contexts = [(p["eventTitle"], p["endDate"], p["winner"]) for p in body["positions"]["open"]]
    assert contexts == [
        (None, None, None),
        ("Fed decision in October?", NOW - 86_400, "No"),
        ("Fed decision in October?", NOW + 86_400, None),
    ]
    resolved = [p["resolvedAt"] for p in body["positions"]["open"]]
    assert resolved[0] is None and resolved[1] >= NOW and resolved[2] is None


def test_an_agent_not_on_the_board_is_an_empty_404():
    conn = fresh_test_conn()
    _user_id, idle, _key = TableWrite.create_user(conn, email="idle@example.com", password_hash="x", handle="Idle")
    conn.close()
    with TestClient(app) as client:
        for address in (idle.address, "0x" + "ab" * 20, "nonsense"):
            resp = client.get(f"/agents/{address}")
            assert resp.status_code == 404
            assert resp.content == b""


def _field(conn) -> dict[str, str]:
    returns = {"Alpha": 30.0, "Bravo": 10.0, "Charlie": 9.5, "Delta": 0.0, "Echo": -5.0, "Foxtrot": -20.0}
    field = {name: _agent(conn, name, pct, 12 if name == "Charlie" else 10) for name, pct in returns.items()}
    field["Rookie"] = _agent(conn, "Rookie", 5.0, 3)
    field["Newbie"] = _agent(conn, "Newbie", -1.0, 1)
    return field


def test_a_ranked_agent_sees_its_place_gap_neighbours_and_tags():
    conn = fresh_test_conn()
    _market(conn)
    field = _field(conn)
    conn.close()
    with TestClient(app) as client:
        charlie, alpha, foxtrot, delta = (_profile(client, field[n]) for n in ("Charlie", "Alpha", "Foxtrot", "Delta"))
    assert [p["share"] for p in (charlie, delta, foxtrot)] == [
        "Charlie is #3 of 6 on AgentPit with a +9.50% return on paper money",
        "Delta is #4 of 6 on AgentPit with a 0.00% return on paper money",
        "Foxtrot is #6 of 6 on AgentPit with a −20.00% return on paper money",
    ]
    charlie, alpha, foxtrot = charlie["standing"], alpha["standing"], foxtrot["standing"]
    assert (charlie["place"], charlie["rankedCount"], charlie["warmingCount"], charlie["gap"]) == (3, 6, 2, 0.5)
    assert charlie["placeChange"] is None
    assert charlie["tags"] == ["busiest", "closestBattle"]
    assert [(n["place"], n["name"], n["returnPct"]) for n in charlie["neighbours"]] == [
        (1, "Alpha", 30.0),
        (2, "Bravo", 10.0),
        (3, "Charlie", 9.5),
        (4, "Delta", 0.0),
        (5, "Echo", -5.0),
    ]
    assert (alpha["place"], alpha["gap"], alpha["tags"]) == (1, 20.0, [])
    assert (foxtrot["gap"], [n["place"] for n in foxtrot["neighbours"]]) == (15.0, [2, 3, 4, 5, 6])


def test_a_warming_agent_has_no_place_and_stands_among_the_warming():
    conn = fresh_test_conn()
    _market(conn)
    field = _field(conn)
    conn.close()
    with TestClient(app) as client:
        rookie = _profile(client, field["Rookie"])
        newbie = _profile(client, field["Newbie"])["standing"]
    assert rookie["share"] == "Rookie is warming up on AgentPit, 3 of 10 trades to rank"
    rookie = rookie["standing"]
    assert (rookie["place"], rookie["placeChange"], rookie["gap"], rookie["tags"]) == (None, None, None, [])
    assert [(n["place"], n["name"], n["trades"]) for n in rookie["neighbours"]] == [
        (None, "Rookie", 3),
        (None, "Newbie", 1),
    ]
    assert newbie["tags"] == []


def _open(
    condition_id: str, value: float, sells_for: float, cost: float, avg: float, outcome: str, end: int
) -> PositionWire:
    return PositionWire(
        conditionId=condition_id,
        outcome=outcome,
        avgPrice=avg,
        curPrice=avg,
        initialValue=cost,
        currentValue=value,
        sellableValue=sells_for,
        cashPnl=value - cost,
        endDate=str(end),
    )


def test_the_book_needs_three_open_positions():
    conn = fresh_test_conn()
    _market(conn)
    address = _agent(conn, "Booked", 1.0, 10)
    conn.close()
    day = 86_400
    two = [
        _open(CRYPTO, 30.0, 10.0, 20.0, 0.05, "Yes", NOW + 40 * day),
        _open(UNFILED, 10.0, 9.0, 10.0, 0.95, "No", NOW + 2 * day),
    ]
    three = [*two, _open(CRYPTO, 15.0, 15.0, 10.0, 1.0, "Yes", NOW - day)]
    settled = PositionWire(conditionId=CRYPTO, avgPrice=0.5, initialValue=5.0, currentValue=500.0, settled=True)
    with TestClient(app) as client:
        _holdings[address] = Holdings(valued_at=NOW, cash_raw=0, positions=[*two, settled], closed=[])
        assert _profile(client, address)["book"] is None
        _holdings[address] = Holdings(valued_at=NOW, cash_raw=0, positions=[*three, settled], closed=[])
        body = _profile(client, address)
    assert body["positions"]["count"] == 3
    assert (body["positions"]["mark"], body["positions"]["sellsFor"]) == ("55000000", "34000000")
    assert [(p["value"], p["sellsFor"], p["pnl"], p["category"]) for p in body["positions"]["top"]] == [
        ("30000000", "10000000", "10000000", "Crypto"),
        ("15000000", "15000000", "5000000", "Crypto"),
        ("10000000", "9000000", "0", None),
    ]
    assert body["positions"]["open"] == body["positions"]["top"]
    book = body["book"]
    assert book["mix"] == [
        {"category": "Crypto", "share": 0.75, "count": 2},
        {"category": None, "share": 0.25, "count": 1},
    ]
    bins = [(b["yes"], b["no"]) for b in book["entryPrice"]]
    assert bins[0] == ("20000000", "0") and bins[9] == ("10000000", "10000000")
    assert all(b == ("0", "0") for b in bins[1:9])
    assert book["horizonDays"] == 2


def test_the_record_counts_decided_positions_oldest_first():
    conn = fresh_test_conn()
    _market(conn)
    address = _agent(conn, "Decided", 1.0, 10)
    conn.close()
    won = PositionWire(
        conditionId=CRYPTO, title="Won", outcome="Yes", avgPrice=0.4, curPrice=1.0, cashPnl=10.0, endDate="300", url="https://polymarket.com/market/won"
    )
    split = PositionWire(title="Split", avgPrice=0.0, cashPnl=50.0, endDate="50")
    push = PositionWire(title="Push", avgPrice=0.2, cashPnl=0.3, endDate="60")
    lost = PositionWire(title="Lost", outcome="No", avgPrice=0.5, curPrice=0.0, cashPnl=-20.0, endDate="100")
    unredeemed = PositionWire(title="Unredeemed", avgPrice=0.3, cashPnl=-5.0, settled=True, endDate="200")
    with TestClient(app) as client:
        _holdings[address] = Holdings(
            valued_at=NOW, cash_raw=0, positions=[unredeemed], closed=[won, split, push, lost]
        )
        body = _profile(client, address)
    record = body["record"]
    assert (record["wins"], record["losses"]) == (1, 2)
    assert record["pnls"] == ["-20000000", "-5000000", "10000000"]
    assert record["best"] == {
        "title": "Won", "icon": None, "category": "Crypto", "outcome": "Yes", "entry": 0.4, "exit": 1.0, "pnl": "10000000",
        "url": "https://polymarket.com/market/won", "eventTitle": None, "endDate": None, "resolvedAt": None, "winner": None,
    }
    assert (record["worst"]["title"], record["worst"]["category"], record["worst"]["exit"]) == ("Lost", None, 0.0)
    assert body["positions"]["count"] == 0


def test_the_money_reconciles_to_capital():
    conn = fresh_test_conn()
    _market(conn)
    address = _agent(conn, "Counted", -8.07, 10, invested=35_012_400_000, unrealized=-3_044_190_000)
    conn.close()
    with TestClient(app) as client:
        money = _profile(client, address)["money"]
    assert (money["returnPct"], money["capital"], money["earned"]) == (-8.07, "91930000000", "-8070000000")
    assert money["cash"] == "59961790000"
    assert int(money["cash"]) + int(money["invested"]) + int(money["unrealized"]) == int(money["capital"])
    assert (money["trendStart"], money["trend"]) == (
        datetime.fromtimestamp(NOW - 30, UTC).date().isoformat(),
        ["-8070000000"],
    )
