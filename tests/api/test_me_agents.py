import pytest
from fastapi.testclient import TestClient

from agentpit.api.main import app
from agentpit.services.agent_accounts import AgentAccounts
from tests.db_helpers import fresh_test_db


def _accounts() -> AgentAccounts:
    return AgentAccounts(fresh_test_db(), lambda *_: pytest.fail("agent_for must not onboard"))


def test_me_agents_lists_the_signed_in_person_s_agents(sign_in):
    accounts = _accounts()
    with TestClient(app) as client:
        token = sign_in(client, "owner@example.com")["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/me/agents", headers=headers).json() == []

        owner = token.removeprefix("at-")
        accounts.agent_for(owner, "Claude")
        codex = accounts.agent_for(owner, "Codex")
        accounts.agent_for("user_someone_else", "Claude")

        agents = client.get("/me/agents", headers=headers).json()

    assert [a["app"] for a in agents] == ["Claude", "Codex"]
    assert agents[1] == {
        "handle": codex.handle,
        "app": "Codex",
        "eth_address": codex.eth_address,
        "created_at": codex.created_at,
    }


@pytest.mark.parametrize(
    ("path", "body"),
    [("/me/private-key/code", None), ("/me/private-key", {"code": "123456"})],
)
def test_an_agent_s_key_cannot_be_exported(path, body):
    agent = _accounts().agent_for("user_owner", "Claude")

    with TestClient(app) as client:
        resp = client.post(path, json=body, headers={"X-API-Key": agent.api_key})

    assert resp.status_code == 400
    assert resp.json()["detail"] == "an agent's key cannot be exported"
