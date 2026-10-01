import pytest
from fastapi.testclient import TestClient

from agentpit.api.main import app
from agentpit.db.table_read import TableRead
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
    }


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
    assert listed == [{k: v for k, v in agent.items() if k != "api_key"}]
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
