import json
import time
import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from agentpit.api.deps import get_onchain_admin
from agentpit.api.main import app
from agentpit.datastructures.bet import Bet, BetStatus
from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.market import Market
from agentpit.datastructures.market_state import MarketState
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.services.account_service import AccountService
from agentpit.services.leaderboard_service import RANK_FLOOR, Holdings, _holdings
from tests.db_helpers import fresh_test_conn, fresh_test_db

G = 100_000_000_000
NOW = int(time.time())
CRYPTO = "0x" + "ab" * 32
UNFILED = "0x" + "cd" * 32


class _NoChain:
    def balances(self, address: str, token_ids: list[int]) -> tuple[int, int, list[int]]:
        raise AssertionError("a profile must not read the chain once the pass has valued the account")


@pytest.fixture(autouse=True)
def _no_chain():
    previous = app.dependency_overrides[get_onchain_admin]
    app.dependency_overrides[get_onchain_admin] = lambda: _NoChain()
    yield
    app.dependency_overrides[get_onchain_admin] = previous


def _market(conn, polymarket_id: int | None = None) -> Market:
    event = TableWrite.upsert_event(conn, slug="btc", title="Bitcoin", category="Crypto")
    return TableWrite.create_market(
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


def _agent(
    conn, handle: str, return_pct: float, trades: int, *, markets: int = 1, owner: str | None = None
) -> str:
    """A snapshotted agent with `trades` fills of 4 Yes at 25¢ over `markets` markets, the first
    in CRYPTO and the newest at `NOW - 60`, whose holdings the pass has already kept."""
    user_id, acct, _key = TableWrite.create_user(
        conn, email=f"{handle}@example.com", password_hash="x", handle=handle, owner_workos_id=owner
    )
    for k in range(trades):
        _buy(conn, acct.address, f"0x{k % markets:064x}" if k % markets else CRYPTO, "btc-y", NOW - 60 - k)
    TableWrite.insert_account_snapshot(
        conn, user_id, NOW - 30, G + round(return_pct * G / 100), G, 0, 0
    )
    _holdings[acct.address] = Holdings(valued_at=NOW - 30, cash_raw=0, positions=[], closed=[])
    return acct.address


def _buy(conn, address: str, condition_id: str, asset: str, at: int) -> None:
    """4 shares of `asset` at 25¢."""
    user = TableRead.get_user_by_eth_address(conn, address)
    assert user is not None
    conn.execute(
        "INSERT INTO trades (TRADE_ID, MARKET, ASSET_ID, SIDE, PRICE, TRADE_SIZE, MATCH_KIND, STATUS, "
        "TAKER_API_KEY, MATCH_TIME) VALUES (%s, %s, %s, 'BUY', 250000, 4000000, 'NORMAL', 'CONFIRMED', %s, %s)",
        (uuid.uuid4().hex, condition_id, asset, user.api_key, at),
    )


def _value(address: str) -> None:
    """Holdings as the pass keeps them, with the bets read from the agent's fills."""
    bets = AccountService(fresh_test_db(), onchain=None).list_bets(address, [], NOW, [], 0)
    _holdings[address] = Holdings(valued_at=NOW - 30, cash_raw=0, positions=[], closed=[], bets=bets)


def _bet(n: int, status: BetStatus, pnl: int) -> Bet:
    return Bet(
        id=str(n), conditionId=CRYPTO, title="Will Bitcoin dip?", icon=None, url=None, outcome="Yes", status=status,
        openedAt=NOW - n, closedAt=None if status == "open" else NOW - n, buys=1, sells=0, entry=0.25, exit=None,
        shares=4.0, sold=0.0, held=0.0, cost="1000000", back="0", value="0", sellsFor="0", pnl=str(pnl), fills=[], prices=None,
    )


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
    assert (body["name"], body["trades"], body["markets"], body["valuedAt"]) == ("Solo", 1, 1, NOW - 30)
    assert body["runner"] == {"slug": "api", "label": "API", "host": None}


def test_the_owner_name_shows_on_two_board_agents_never_on_one_and_never_when_hidden():
    conn = fresh_test_conn()
    _market(conn)
    solo = _agent(conn, "Solo", 0.0, 1, owner="user_solo")
    pair = [_agent(conn, name, 0.0, 1, owner="user_pair") for name in ("Left", "Right")]
    keyed = _agent(conn, "Keyed", 0.0, 1)
    with TestClient(app) as client:
        bodies = [client.get(f"/agents/{a}").text for a in (solo, *pair, keyed)]
        TableWrite.update_owner(conn, "user_pair", None, False)
        hidden = [_profile(client, a)["owner"] for a in pair]
    owner = TableRead.get_owner(conn, "user_pair")
    conn.close()
    assert owner is not None
    assert [json.loads(b)["owner"] for b in bodies] == [None, owner.name, owner.name, None]
    assert not any("@" in b or "user_" in b for b in bodies)
    assert hidden == [None, None]


def test_the_profile_lists_bets_instead_of_positions_book_record_and_activity(monkeypatch):
    conn = fresh_test_conn()
    _market(conn, polymarket_id=7)
    address = _agent(conn, "Linked", 0.0, 1)
    conn.close()
    _value(address)

    def unread(*_args, **_kwargs):
        raise AssertionError("a profile must not read fills per request")

    monkeypatch.setattr(AccountService, "list_activity", unread)
    with TestClient(app) as client:
        body = _profile(client, address)
    assert not {"positions", "book", "record", "activity"} & set(body)
    (bet,) = body["bets"]
    assert set(bet) == {
        "id", "title", "icon", "url", "category", "outcome", "status", "openedAt", "closedAt", "buys", "sells",
        "entry", "exit", "shares", "sold", "held", "cost", "back", "value", "sellsFor", "pnl",
        "eventTitle", "endDate", "resolvedAt", "winner", "kickoff", "trading", "fills", "prices",
    }
    assert (bet["id"], bet["url"], bet["category"], bet["status"]) == ("btc-y", "https://polymarket.com/market/btc", "Crypto", "open")


def test_each_bet_carries_its_event_and_status():
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
    TableWrite.set_market_state(conn, cut.market_id, MarketState.ACTIVE, MarketState.RESOLVED, (0, 1))
    address = _agent(conn, "Contexts", 0.0, 1)
    _buy(conn, address, cut.condition_id.value, "cut-y", NOW - 120)
    _buy(conn, address, hold.condition_id.value, "hold-y", NOW - 90)
    conn.close()
    _value(address)
    with TestClient(app) as client:
        bets = _profile(client, address)["bets"]
    assert [(b["id"], b["status"], b["category"], b["eventTitle"], b["endDate"], b["winner"]) for b in bets] == [
        ("btc-y", "open", "Crypto", None, None, None),
        ("hold-y", "open", "Business", "Fed decision in October?", NOW + 86_400, None),
        ("cut-y", "lost", "Business", "Fed decision in October?", NOW - 86_400, "No"),
    ]
    assert bets[0]["resolvedAt"] is None and bets[2]["resolvedAt"] >= NOW


def test_the_profile_lists_every_open_bet_and_the_newest_100_closed_and_counts_them_all():
    conn = fresh_test_conn()
    _market(conn)
    address = _agent(conn, "Busy", 0.0, 1)
    conn.close()
    opened = [_bet(n, "open", 5) for n in range(3)]
    closed = [_bet(n, "won" if n % 2 else "lost", 1 if n % 2 else -1) for n in range(3, 106)]
    _holdings[address] = Holdings(valued_at=NOW, cash_raw=0, positions=[], closed=[], bets=[*opened, *closed])
    with TestClient(app) as client:
        body = _profile(client, address)
    assert [b["id"] for b in body["bets"]] == [str(n) for n in range(103)]
    assert body["betTotals"] == {
        "open": {"n": 3, "pnl": "15"},
        "won": {"n": 52, "pnl": "52"},
        "lost": {"n": 51, "pnl": "-51"},
        "sold": {"n": 0, "pnl": "0"},
        "split": {"n": 0, "pnl": "0"},
    }


def test_the_bet_totals_add_up_to_earned():
    conn = fresh_test_conn()
    m = _market(conn)
    address = _agent(conn, "Settled", 0.003, 1)
    TableWrite.set_market_state(conn, m.market_id, MarketState.ACTIVE, MarketState.RESOLVED, (1, 0))
    user = TableRead.get_user_by_eth_address(conn, address)
    assert user is not None
    TableWrite.log_transaction(conn, user.api_key, "REDEEM", m.market_id, {"collateral_amount": 4_000_000})
    conn.close()
    _value(address)
    with TestClient(app) as client:
        body = _profile(client, address)
    assert body["money"]["earned"] == "3000000"
    assert sum(int(t["pnl"]) for t in body["betTotals"].values()) == int(body["money"]["earned"])
    assert body["betTotals"]["won"] == {"n": 1, "pnl": "3000000"}


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
    field = {name: _agent(conn, name, pct, 12 if name == "Charlie" else 10, markets=RANK_FLOOR) for name, pct in returns.items()}
    field["Rookie"] = _agent(conn, "Rookie", 5.0, 10, markets=RANK_FLOOR - 1)
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
    assert rookie["share"] == f"Rookie is warming up on AgentPit, {RANK_FLOOR - 1} of {RANK_FLOOR} markets to rank"
    rookie = rookie["standing"]
    assert (rookie["place"], rookie["placeChange"], rookie["gap"], rookie["tags"]) == (None, None, None, [])
    assert [(n["place"], n["name"], n["trades"]) for n in rookie["neighbours"]] == [
        (None, "Rookie", 10),
        (None, "Newbie", 1),
    ]
    assert newbie["tags"] == []


def test_the_money_reads_the_latest_snapshot():
    conn = fresh_test_conn()
    _market(conn)
    address = _agent(conn, "Counted", -8.07, 10)
    conn.close()
    with TestClient(app) as client:
        money = _profile(client, address)["money"]
    assert (money["returnPct"], money["capital"], money["earned"]) == (-8.07, "91930000000", "-8070000000")
    assert (money["trendStart"], money["trend"]) == (
        datetime.fromtimestamp(NOW - 30, UTC).date().isoformat(),
        ["-8070000000"],
    )
