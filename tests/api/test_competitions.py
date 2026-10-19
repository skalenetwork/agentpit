import time

import pytest
from fastapi.testclient import TestClient

from agentpit.api.main import app
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.services.agent_accounts import AgentAccounts
from tests.db_helpers import fresh_test_conn, fresh_test_db

ADMIN = {"X-Admin-Token": "dev-admin-token"}
NOW = int(time.time())
BODY = {
    "title": "October test",
    "startsAt": NOW + 2 * 86_400,
    "endsAt": NOW + 5 * 86_400,
    "prizeToken": "SKL",
    "prizes": [{"usd": 0, "tokens": 0}],
}


def test_a_competition_the_admin_puts_is_public_with_its_prizes():
    pilot = {**BODY, "title": "Pilot", "startsAt": NOW + 3 * 86_400, "prizes": [{"usd": 700, "tokens": 100}, {"tokens": 100}]}
    with TestClient(app) as client:
        created = client.put("/admin/competitions/october-test", json=BODY, headers=ADMIN)
        client.put("/admin/competitions/pilot", json=pilot, headers=ADMIN)
        listed = client.get("/competitions").json()["competitions"]
        page = client.get("/competitions/october-test").json()
        missing = client.get("/competitions/nope")

    assert created.json() == {
        "slug": "october-test",
        "title": "October test",
        "startsAt": BODY["startsAt"],
        "locksAt": BODY["startsAt"] - 86_400,
        "endsAt": BODY["endsAt"],
        "phase": "upcoming",
        "prizeToken": "SKL",
        "prizes": [{"usd": 0, "tokens": 0}],
        "entrants": 0,
    }
    assert [c["slug"] for c in listed] == ["pilot", "october-test"]
    assert listed[0]["prizes"] == [{"usd": 700, "tokens": 100}, {"usd": 0, "tokens": 100}]
    assert page == {**created.json(), "final": False, "standings": []}
    assert missing.status_code == 404


def test_an_owner_enters_and_leaves_with_only_their_own_agents(sign_in):
    accounts = AgentAccounts(fresh_test_db(), lambda *_: pytest.fail("must not onboard"))
    with TestClient(app) as client:
        client.put("/admin/competitions/october-test", json=BODY, headers=ADMIN)
        token = sign_in(client, "owner@example.com")["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        owner = token.removeprefix("at-")
        mine = accounts.agent_for(owner, "Claude", None, None)
        other = accounts.agent_for("user_someone_else", "Claude", None, None)
        conn = fresh_test_conn()
        TableWrite.mark_user_onboarded(conn, mine.user_id)
        human = TableRead.get_user_by_workos_id(conn, owner)
        conn.close()
        assert human is not None
        path = f"/me/agents/{mine.eth_address}/competitions/october-test"

        entered = client.post(path, headers=headers)
        listed = client.get("/me/agents", headers=headers).json()
        page = client.get("/competitions/october-test").json()
        keyed = client.post(path, headers={"X-API-Key": human.api_key})
        foreign = client.post(f"/me/agents/{other.eth_address}/competitions/october-test", headers=headers)
        left = client.delete(path, headers=headers)
        after = client.get("/me/agents", headers=headers).json()

    assert entered.status_code == 204 and listed[0]["competitions"] == ["october-test"]
    assert (page["entrants"], page["standings"]) == (
        1,
        [
            {
                "rank": None,
                "name": mine.handle,
                "address": mine.eth_address,
                "runner": {"slug": "claude", "label": "Claude", "host": None},
                "listed": False,
                "runStartsAt": BODY["startsAt"],
                "earned": "0",
                "returnPct": 0.0,
                "trades": 0,
                "disqualified": False,
            }
        ],
    )
    assert (keyed.status_code, foreign.status_code) == (403, 404)
    assert left.status_code == 204 and after[0]["competitions"] == []


def test_the_admin_put_is_validated_guarded_and_keeps_a_started_window():
    bad = [
        ("Bad_Slug", BODY),
        ("x" * 65, BODY),
        ("ok", {**BODY, "title": ""}),
        ("ok", {**BODY, "endsAt": BODY["startsAt"]}),
        ("ok", {**BODY, "startsAt": 0}),
        ("ok", {**BODY, "endsAt": 10**10}),
        ("ok", {**BODY, "prizes": []}),
        ("ok", {**BODY, "prizes": [{"usd": -1}]}),
    ]
    started = {**BODY, "startsAt": NOW - 60}
    with TestClient(app) as client:
        unauthorized = client.put("/admin/competitions/ok", json=BODY)
        codes = [client.put(f"/admin/competitions/{slug}", json=body, headers=ADMIN).status_code for slug, body in bad]
        client.put("/admin/competitions/live", json=started, headers=ADMIN)
        moved = client.put("/admin/competitions/live", json={**started, "endsAt": BODY["endsAt"] + 1}, headers=ADMIN)
        renamed = client.put("/admin/competitions/live", json={**started, "title": "Renamed"}, headers=ADMIN)

    assert unauthorized.status_code == 401
    assert codes == [422] * len(bad)
    assert moved.status_code == 400 and "cannot change" in moved.json()["detail"]
    assert (renamed.json()["title"], renamed.json()["phase"]) == ("Renamed", "live")
