import time
import uuid

import pytest
from fastapi.testclient import TestClient

from agentpit.api.main import app
from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.market_state import MarketState
from agentpit.datastructures.user import User
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.services.agent_accounts import AgentAccounts
from agentpit.services.leaderboard_service import RANK_FLOOR
from tests.db_helpers import fresh_test_conn, fresh_test_db

G = 100_000_000_000
NOW = int(time.time())
CRYPTO = "0x" + "ab" * 32


def _accounts() -> AgentAccounts:
    return AgentAccounts(fresh_test_db(), lambda *_: pytest.fail("agent_for must not onboard"))


def _traded(conn, agent: User, trades: int, earned: int) -> None:
    """`trades` fills, the newest at `NOW - 60`, and a snapshot `earned` above the grant."""
    for k in range(trades):
        conn.execute(
            "INSERT INTO trades (TRADE_ID, MARKET, ASSET_ID, SIDE, PRICE, TRADE_SIZE, MATCH_KIND, STATUS, "
            "TAKER_API_KEY, MATCH_TIME) VALUES (%s, %s, 'btc-y', 'BUY', 250000, 4000000, 'NORMAL', 'CONFIRMED', %s, %s)",
            (uuid.uuid4().hex, CRYPTO, agent.api_key, NOW - 60 - k),
        )
    TableWrite.insert_account_snapshot(conn, agent.user_id, NOW - 30, G + earned, G)


def test_me_agents_lists_the_signed_in_person_s_agents(sign_in):
    accounts = _accounts()
    with TestClient(app) as client:
        token = sign_in(client, "owner@example.com")["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/me/agents", headers=headers).json() == []

        owner = token.removeprefix("at-")
        accounts.agent_for(owner, "Claude", None, None)
        codex = accounts.agent_for(owner, "Codex", None, "localhost")
        accounts.agent_for("user_someone_else", "Claude", None, None)

        agents = client.get("/me/agents", headers=headers).json()

    assert [a["runner"]["label"] for a in agents] == ["Claude", "Codex"]
    assert agents[1] == {
        "handle": codex.handle,
        "eth_address": codex.eth_address,
        "created_at": codex.created_at,
        "runner": {"slug": "codex", "label": "Codex", "host": "Self-hosted"},
        "equity": str(G),
        "trades": 0,
        "last_trade_at": None,
        "earned": "0",
        "return_pct": 0.0,
        "place": None,
        "place_change": None,
        "trend": [],
        "trend_start": None,
    }


def test_me_agents_carries_each_agent_s_board_figures(sign_in):
    accounts = _accounts()
    with TestClient(app) as client:
        token = sign_in(client, "owner@example.com")["access_token"]
        owner = token.removeprefix("at-")
        ranked = accounts.agent_for(owner, "Claude", None, None)
        warming = accounts.agent_for(owner, "Codex", None, None)
        rival = accounts.agent_for("user_someone_else", "Claude", None, None)
        conn = fresh_test_conn()
        _traded(conn, ranked, RANK_FLOOR, 5 * G // 100)
        _traded(conn, warming, RANK_FLOOR - 1, 20 * G // 100)
        _traded(conn, rival, RANK_FLOOR, 10 * G // 100)
        conn.close()

        agents = client.get("/me/agents", headers={"Authorization": f"Bearer {token}"}).json()

    figures = ("trades", "last_trade_at", "equity", "earned", "return_pct", "place", "place_change", "trend")
    assert [{k: a[k] for k in figures} for a in agents] == [
        {
            "trades": RANK_FLOOR,
            "last_trade_at": NOW - 60,
            "equity": "105000000000",
            "earned": "5000000000",
            "return_pct": 5.0,
            "place": 2,
            "place_change": None,
            "trend": ["5000000000"],
        },
        {
            "trades": RANK_FLOOR - 1,
            "last_trade_at": NOW - 60,
            "equity": "120000000000",
            "earned": "20000000000",
            "return_pct": 20.0,
            "place": None,
            "place_change": None,
            "trend": ["20000000000"],
        },
    ]


def _owner(client: TestClient, sign_in, email: str) -> tuple[dict[str, str], str]:
    token = sign_in(client, email)["access_token"]
    return {"Authorization": f"Bearer {token}"}, token.removeprefix("at-")


def test_a_new_api_agent_shows_its_key_once_and_trades_by_it(sign_in):
    with TestClient(app) as client:
        headers, _ = _owner(client, sign_in, "owner@example.com")

        created = client.post("/me/agents", headers=headers)
        listed = client.get("/me/agents", headers=headers).json()
        me = client.get("/me", headers={"X-API-Key": created.json()["api_key"]})

    assert created.status_code == 201
    agent = created.json()
    assert agent["runner"] == {"slug": "api", "label": "API", "host": None}
    summary = {k: v for k, v in agent.items() if k != "api_key"}
    assert [{k: a[k] for k in summary} for a in listed] == [summary]
    assert me.status_code == 200 and me.json()["handle"] == agent["handle"]


def test_the_settings_key_cannot_manage_agents(sign_in):
    with TestClient(app) as client:
        _, owner = _owner(client, sign_in, "owner@example.com")
        with fresh_test_db().read() as conn:
            human = TableRead.get_user_by_workos_id(conn, owner)
        assert human is not None

        resp = client.post("/me/agents", headers={"X-API-Key": human.api_key})

    assert resp.status_code == 403


def test_an_owner_renames_only_their_own_agents(sign_in):
    accounts = _accounts()
    with TestClient(app) as client:
        headers, owner = _owner(client, sign_in, "owner@example.com")
        mine = accounts.agent_for(owner, "Claude", None, None)
        other = accounts.agent_for("user_someone_else", "Claude", None, None)

        renamed = client.patch(f"/me/agents/{mine.eth_address.lower()}", json={"handle": "Racer"}, headers=headers)
        taken = client.patch(f"/me/agents/{mine.eth_address}", json={"handle": other.handle}, headers=headers)
        foreign = client.patch(f"/me/agents/{other.eth_address}", json={"handle": "Thief"}, headers=headers)

    assert renamed.status_code == 200 and renamed.json()["handle"] == "Racer"
    assert taken.status_code == 409
    assert foreign.status_code == 404


def test_an_owner_reads_the_open_orders_of_only_their_own_agents(sign_in):
    accounts = _accounts()
    with TestClient(app) as client:
        headers, owner = _owner(client, sign_in, "owner@example.com")
        mine = accounts.agent_for(owner, "Claude", None, None)
        other = accounts.agent_for("user_someone_else", "Claude", None, None)
        conn = fresh_test_conn()
        TableWrite.create_market(
            conn,
            CreateMarketRequest(
                question="Will Bitcoin dip?",
                description="d",
                erc1155_tokens=[("btc-y", "Yes"), ("btc-n", "No")],
                slug="btc",
                condition_id=ConditionId(CRYPTO),
                state=MarketState.ACTIVE,
            ),
            is_polygon_market=False,
        )
        conn.execute(
            "INSERT INTO orders (ORDER_ID, API_KEY, TOKEN_ID, SIDE, PRICE, MAKER, MAKER_AMOUNT, TAKER_AMOUNT, "
            "REMAINING_AMOUNT, EXPIRATION, ORDER_TYPE, CREATED_AT) "
            "VALUES ('o1', %s, 'btc-y', 'BUY', 250000, %s, 1000000, 4000000, 3000000, 0, 'GTC', %s)",
            (mine.api_key, mine.eth_address, NOW),
        )
        human = TableRead.get_user_by_workos_id(conn, owner)
        conn.close()
        assert human is not None

        orders = client.get(f"/me/agents/{mine.eth_address.lower()}/orders", headers=headers)
        foreign = client.get(f"/me/agents/{other.eth_address}/orders", headers=headers)
        keyed = client.get(f"/me/agents/{mine.eth_address}/orders", headers={"X-API-Key": human.api_key})

    assert orders.status_code == 200 and orders.json() == [
        {
            "id": "o1",
            "status": "LIVE",
            "owner": mine.user_id,
            "maker_address": mine.eth_address,
            "market": CRYPTO,
            "asset_id": "btc-y",
            "side": "BUY",
            "original_size": "4",
            "size_matched": "1",
            "price": "0.25",
            "associate_trades": [],
            "outcome": "Yes",
            "created_at": NOW,
            "expiration": "0",
            "order_type": "GTC",
            "title": "Will Bitcoin dip?",
        }
    ]
    assert foreign.status_code == 404
    assert keyed.status_code == 403


def test_a_deleted_agent_leaves_the_list_and_its_key_stops_working(sign_in):
    accounts = _accounts()
    with TestClient(app) as client:
        headers, owner = _owner(client, sign_in, "owner@example.com")
        agent = accounts.agent_for(owner, "Claude", None, None)

        deleted = client.delete(f"/me/agents/{agent.eth_address}", headers=headers)
        again = client.delete(f"/me/agents/{agent.eth_address}", headers=headers)
        listed = client.get("/me/agents", headers=headers).json()
        me = client.get("/me", headers={"X-API-Key": agent.api_key})

    assert deleted.status_code == 204
    assert again.status_code == 404
    assert listed == []
    assert me.status_code == 401


@pytest.mark.parametrize(
    ("path", "body"),
    [("/me/private-key/code", None), ("/me/private-key", {"code": "123456"})],
)
def test_an_agent_s_key_cannot_be_exported(path, body):
    agent = _accounts().agent_for("user_owner", "Claude", None, None)

    with TestClient(app) as client:
        resp = client.post(path, json=body, headers={"X-API-Key": agent.api_key})

    assert resp.status_code == 400
    assert resp.json()["detail"] == "an agent's key cannot be exported"
