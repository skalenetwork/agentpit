from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentpit.api.app import create_app
from agentpit.config import Settings
from agentpit.datastructures.user import User
from agentpit.db.table_read import TableRead
from agentpit.services.agent_accounts import AgentAccounts
from tests.db_helpers import fresh_test_db

MODERN = "2026-07-28"
WORKOS = {"workos_api_key": "sk_test", "workos_client_id": "client_test", "workos_authkit_domain": "https://test.authkit.app"}
app = create_app(Settings(**WORKOS, mcp_url="http://testserver/mcp"))
plain = create_app(Settings(workos_authkit_domain=""))


def _agent() -> User:
    return AgentAccounts(fresh_test_db(), lambda *_: pytest.fail("agent_for must not onboard")).agent_for(
        "user_owner", "Claude"
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


def test_modern_tools_list_carries_seven_annotated_tools():
    key = _agent().api_key
    with TestClient(app) as client:
        result = _call(client, key, "tools/list", {})

    tools = {t["name"]: t for t in result["tools"]}
    assert set(tools) == {"search_markets", "get_market", "trade", "cancel", "portfolio", "top_up", "leaderboard"}
    assert all(t["title"] and t["annotations"]["openWorldHint"] is False for t in tools.values())
    assert tools["portfolio"]["annotations"]["readOnlyHint"] is True
    assert tools["trade"]["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    }
    assert tools["cancel"]["annotations"]["destructiveHint"] is True
    assert (result["ttlMs"], result["cacheScope"]) == (3_600_000, "public")


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
    assert "https://agentpit.dev/skill.md" in instructions and len(instructions) < 512
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
    with fresh_test_db().read() as conn:
        stored = TableRead.get_user_by_userid(conn, agent.user_id)
    assert stored is not None and stored.onboarded_at is not None


def test_a_second_lifespan_still_serves():
    key = _agent().api_key
    for _ in range(2):
        with TestClient(app) as client:
            assert len(_call(client, key, "tools/list", {})["tools"]) == 7


def test_mcp_adds_no_openapi_paths():
    with TestClient(app) as client, TestClient(plain) as bare:
        assert client.get("/openapi.json").json()["paths"] == bare.get("/openapi.json").json()["paths"]


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
