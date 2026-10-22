import json
import time
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentpit.api.app import create_app
from agentpit.api.mcp_server import CARD
from agentpit.auth.mcp_tokens import AgentVerifier
from agentpit.config import Settings
from agentpit.datastructures.competition import Competition
from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.market_state import MarketState
from agentpit.datastructures.user import User
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import AgentLimitError
from agentpit.services.agent_accounts import AgentAccounts
from tests.db_helpers import fresh_test_db

MODERN = "2026-07-28"
WORKOS = {"workos_api_key": "sk_test", "workos_client_id": "client_test", "workos_authkit_domain": "https://test.authkit.app"}
app = create_app(Settings(**WORKOS, mcp_url="http://testserver/mcp"))
plain = create_app(Settings(workos_authkit_domain=""))


def _agent() -> User:
    return AgentAccounts(fresh_test_db(), lambda *_: pytest.fail("agent_for must not onboard")).agent_for(
        "user_owner", "Claude", None, None
    )


def _call(client: TestClient, key: str, method: str, params: dict[str, Any], name: str | None = None) -> Any:
    meta = {"io.modelcontextprotocol/protocolVersion": MODERN, "io.modelcontextprotocol/clientCapabilities": {}}
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": MODERN,
        "Mcp-Method": method,
        **({"Mcp-Name": name} if name else {}),
    }
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {**params, "_meta": meta}}
    resp = client.post("/mcp", json=body, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]


def test_protected_resource_metadata_names_authkit():
    with TestClient(app) as client:
        resp = client.get("/.well-known/oauth-protected-resource/mcp")

    assert resp.status_code == 200
    assert resp.json()["resource"] == "http://testserver/mcp"
    assert resp.json()["authorization_servers"] == ["https://test.authkit.app"]


def test_a_bad_credential_gets_401_pointing_at_the_metadata():
    with TestClient(app) as client:
        resp = client.post("/mcp", json={}, headers={"Authorization": "Bearer nope"})

    assert resp.status_code == 401
    assert 'resource_metadata="http://testserver/.well-known/oauth-protected-resource/mcp"' in resp.headers[
        "www-authenticate"
    ]


def test_get_is_not_allowed():
    with TestClient(app) as client:
        resp = client.get("/mcp")

    assert resp.status_code == 405
    assert resp.json() == {"detail": "Method Not Allowed"}


def test_modern_tools_list_carries_nine_annotated_tools():
    key = _agent().api_key
    with TestClient(app) as client:
        result = _call(client, key, "tools/list", {})

    tools = {t["name"]: t for t in result["tools"]}
    assert set(tools) == {
        "search_markets", "get_market", "trade", "cancel", "portfolio", "top_up", "rename", "enroll", "leaderboard"
    }
    assert all(t["title"] and t["annotations"]["openWorldHint"] is False for t in tools.values())
    assert tools["portfolio"]["annotations"]["readOnlyHint"] is True
    assert tools["trade"]["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    }
    assert tools["cancel"]["annotations"]["destructiveHint"] is False
    assert tools["enroll"]["annotations"]["idempotentHint"] is True
    assert (result["ttlMs"], result["cacheScope"]) == (3_600_000, "public")


def test_market_and_portfolio_tools_render_the_card():
    key = _agent().api_key
    with TestClient(app) as client:
        tools = _call(client, key, "tools/list", {})["tools"]
        read = _call(client, key, "resources/read", {"uri": CARD}, CARD)

    card_tools = {t["name"]: t["_meta"] for t in tools if "_meta" in t}
    assert card_tools == dict.fromkeys(["search_markets", "get_market", "portfolio"], {"ui": {"resourceUri": CARD}})
    [card] = read["contents"]
    assert card["mimeType"] == "text/html;profile=mcp-app"
    assert card["text"].startswith("<!doctype html>")
    assert card["_meta"]["ui"] == {
        "csp": {"resourceDomains": ["https://agentpit.dev", "https://assets.claude.ai"]},
        "prefersBorder": False,
    }
    assert (read["ttlMs"], read["cacheScope"]) == (3_600_000, "public")


def test_search_markets_replies_with_the_polymarket_url_the_card_opens_and_compact_text(house_book):
    key = _agent().api_key
    with fresh_test_db().write() as conn:
        event = TableWrite.upsert_event(conn, slug="ev-rain", title="Will it rain?", category="Weather")
        market = TableWrite.create_market(
            conn,
            CreateMarketRequest(
                question="Will it rain?",
                description="d",
                erc1155_tokens=[("rain-y", "Yes"), ("rain-n", "No")],
                slug="will-it-rain",
                condition_id=ConditionId("0x" + "ef" * 32),
                state=MarketState.ACTIVE,
                event_id=event.event_id,
                polymarket_id=42,
            ),
            is_polygon_market=False,
        )
    house_book(market.erc1155_tokens[0][0], bids=(("0.4", "1"),), asks=(("0.6", "1"),))
    with TestClient(app) as client:
        found = _call(client, key, "tools/call", {"name": "search_markets", "arguments": {}}, "search_markets")
        read = _call(client, key, "resources/read", {"uri": CARD}, CARD)
    [card] = found["structuredContent"]["markets"]
    text = found["content"][0]["text"]
    assert (card["market"], card["url"], card["starts_at"]) == ("will-it-rain", "https://polymarket.com/market/will-it-rain", None)
    assert "\n" not in text and "null" not in text and json.loads(text)["markets"][0]["url"] == card["url"]
    assert "openLink(url)" in read["contents"][0]["text"]


def test_search_markets_refuses_a_query_over_200_characters_and_still_serves_a_short_one():
    key = _agent().api_key
    with TestClient(app) as client:
        long = _call(client, key, "tools/call", {"name": "search_markets", "arguments": {"query": "x" * 201}}, "search_markets")
        edge = _call(client, key, "tools/call", {"name": "search_markets", "arguments": {"query": "x" * 200}}, "search_markets")
        short = _call(client, key, "tools/call", {"name": "search_markets", "arguments": {"query": "bitcoin"}}, "search_markets")

    assert long["isError"] is True
    assert (edge["isError"], short["isError"]) == (False, False)


def test_trade_refuses_an_amount_above_1e15_with_the_validation_message(house_book):
    key = _agent().api_key
    with fresh_test_db().write() as conn:
        event = TableWrite.upsert_event(conn, slug="ev-rain", title="Will it rain?", category="Weather")
        market = TableWrite.create_market(
            conn,
            CreateMarketRequest(
                question="Will it rain?",
                description="d",
                erc1155_tokens=[("rain-y", "Yes"), ("rain-n", "No")],
                slug="will-it-rain",
                condition_id=ConditionId("0x" + "ef" * 32),
                state=MarketState.ACTIVE,
                event_id=event.event_id,
                polymarket_id=42,
            ),
            is_polygon_market=False,
        )
    house_book(market.erc1155_tokens[0][0], bids=(("0.4", "1"),), asks=(("0.6", "1"),))
    with TestClient(app) as client:
        result = _call(
            client,
            key,
            "tools/call",
            {"name": "trade", "arguments": {"market": "will-it-rain", "outcome": "Yes", "side": "buy", "shares": 1e303}},
            "trade",
        )

    text = result["content"][0]["text"]
    assert result["isError"] is True
    assert "less than or equal" in text and text != "Error executing tool trade"


def test_legacy_initialize_carries_the_instructions_and_served_icons():
    key = _agent().api_key
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
    }
    with TestClient(app) as client:
        resp = client.post(
            "/mcp",
            json=body,
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json, text/event-stream"},
        )

        icons = resp.json()["result"]["serverInfo"]["icons"]
        served = [client.get(icon["src"]) for icon in icons]

    assert resp.status_code == 200, resp.text
    instructions = resp.json()["result"]["instructions"]
    assert "https://agentpit.dev/skill.md" in instructions and "profile_url" in instructions and len(instructions) < 512
    assert [(i["theme"], i["mimeType"], i["sizes"]) for i in icons] == [
        ("light", "image/png", ["96x96"]),
        ("dark", "image/png", ["96x96"]),
    ]
    assert all(r.status_code == 200 and r.content.startswith(b"\x89PNG") for r in served)


def test_portfolio_onboards_a_fresh_agent_on_first_call():
    agent = _agent()
    assert agent.onboarded_at is None

    with TestClient(app) as client:
        result = _call(client, agent.api_key, "tools/call", {"name": "portfolio", "arguments": {}}, "portfolio")

    assert result["isError"] is False
    assert result["structuredContent"]["app"] == "Claude"
    assert result["structuredContent"]["cash_usd"] > 0
    assert (result["structuredContent"]["profile_url"], result["structuredContent"]["trades_to_rank"]) == (None, 10)
    with fresh_test_db().read() as conn:
        stored = TableRead.get_user_by_userid(conn, agent.user_id)
    assert stored is not None and stored.onboarded_at is not None


def test_rename_changes_the_name_and_refuses_a_taken_or_malformed_one():
    db = fresh_test_db()
    accounts = AgentAccounts(db, lambda *_: pytest.fail("agent_for must not onboard"))
    agent = accounts.agent_for("user_owner", "Claude", None, None)
    other = accounts.agent_for("user_someone_else", "Claude", None, None)
    with db.write() as conn:
        TableWrite.mark_user_onboarded(conn, agent.user_id)

    with TestClient(app) as client:
        renamed, taken, malformed = (
            _call(client, agent.api_key, "tools/call", {"name": "rename", "arguments": {"name": name}}, "rename")
            for name in ("Ziggy", other.handle, "two words")
        )

    assert renamed["isError"] is False and renamed["structuredContent"] == {"agent": "Ziggy"}
    assert taken["isError"] is True and "already in use" in taken["content"][0]["text"]
    assert malformed["isError"] is True
    with db.read() as conn:
        stored = TableRead.get_user_by_userid(conn, agent.user_id)
    assert stored is not None and stored.handle == "Ziggy"


def test_enroll_enters_once_and_refuses_another_agent_of_the_same_owner():
    db = fresh_test_db()
    accounts = AgentAccounts(db, lambda *_: pytest.fail("agent_for must not onboard"))
    agent = accounts.agent_for("user_owner", "Claude", None, None)
    other = accounts.agent_for("user_owner", "Codex", None, None)
    starts = int(time.time()) + 2 * 86_400
    competition = Competition(slug="october-test", title="October test", starts_at=starts, ends_at=starts + 86_400, prizes=[])
    with db.write() as conn:
        for user in (agent, other):
            TableWrite.mark_user_onboarded(conn, user.user_id)

    with TestClient(app) as client:
        with db.write() as conn:
            TableWrite.save_competition(conn, competition, 0)
        first, again, refused = (
            _call(client, user.api_key, "tools/call", {"name": "enroll", "arguments": {"competition": ref}}, "enroll")
            for user, ref in (
                (agent, "https://agentpit.dev/competitions/october-test"),
                (agent, "october-test"),
                (other, "october-test"),
            )
        )

    entry = first["structuredContent"]
    assert (first["isError"], again["isError"], again["structuredContent"]) == (False, False, entry)
    assert (entry["competition"], entry["url"]) == ("october-test", "https://agentpit.dev/competitions/october-test")
    assert datetime.fromisoformat(entry["run_starts_at"]).timestamp() == starts
    day = datetime.fromtimestamp(starts, UTC)
    assert entry["line"] == f"{agent.handle} is entered in October test, starting {day:%b} {day.day} at {day:%H:%M} UTC"
    assert refused["isError"] is True and "console" in refused["content"][0]["text"]


def test_a_second_lifespan_still_serves():
    key = _agent().api_key
    for _ in range(2):
        with TestClient(app) as client:
            assert len(_call(client, key, "tools/list", {})["tools"]) == 9


def test_mcp_adds_no_openapi_paths():
    with TestClient(app) as client:
        assert client.get("/openapi.json").json()["paths"] == TestClient(plain).get("/openapi.json").json()["paths"]


@pytest.mark.parametrize(
    "settings",
    [
        Settings(workos_authkit_domain=""),
        Settings(**{**WORKOS, "workos_api_key": ""}, mcp_url="http://testserver/mcp"),
        Settings(**WORKOS, mcp_url="http://[::1/mcp"),
        Settings(**WORKOS, mcp_url="https://api.agentpit.dev:abc/mcp"),
    ],
    ids=["no-domain", "no-workos", "bad-ipv6", "bad-port"],
)
def test_without_a_usable_config_there_is_no_mcp_and_rest_still_serves(settings: Settings):
    with TestClient(create_app(settings)) as client:
        assert client.post("/mcp", json={}).status_code == 404
        assert client.get("/.well-known/oauth-protected-resource/mcp").status_code == 404
        assert client.get("/").status_code == 200


def test_with_no_room_for_its_agent_a_tool_says_why_and_reads_still_work(monkeypatch):
    def full(self, token: str):
        raise AgentLimitError("at most 10 agents per account")

    monkeypatch.setattr(AgentVerifier, "_oauth_user", full)
    with TestClient(app) as client:
        refused = _call(client, "a.b.c", "tools/call", {"name": "portfolio", "arguments": {}}, "portfolio")
        board = _call(client, "a.b.c", "tools/call", {"name": "leaderboard", "arguments": {}}, "leaderboard")

    assert refused["isError"] is True
    assert "at most 10 agents per account" in refused["content"][0]["text"]
    assert board["isError"] is False
