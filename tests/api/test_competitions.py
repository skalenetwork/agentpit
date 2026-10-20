import time

import pytest
from fastapi.testclient import TestClient

from agentpit.api.main import app
from agentpit.datastructures.competition import Competition, Prize
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.services.agent_accounts import AgentAccounts
from tests.db_helpers import fresh_test_conn, fresh_test_db

DAY = 86_400
NOW = int(time.time())
TEST = Competition(slug="october-test", title="October test", starts_at=NOW + 2 * DAY, ends_at=NOW + 5 * DAY, prizes=[])


def _save(*competitions: Competition) -> None:
    conn = fresh_test_conn()
    for competition in competitions:
        TableWrite.save_competition(conn, competition, NOW)
    conn.close()


def test_competitions_are_public_with_their_prizes():
    pilot = Competition(
        slug="pilot", title="Pilot", starts_at=NOW + 3 * DAY, ends_at=NOW + 5 * DAY, prizes=[Prize(usd=700, skl=100), Prize(usd=0, skl=100)]
    )
    with TestClient(app) as client:
        _save(TEST, pilot)
        listed = client.get("/competitions").json()["competitions"]
        page = client.get("/competitions/october-test").json()
        missing = client.get("/competitions/nope")

    assert page == {
        "slug": "october-test",
        "title": "October test",
        "startsAt": TEST.starts_at,
        "locksAt": TEST.starts_at - DAY,
        "endsAt": TEST.ends_at,
        "phase": "upcoming",
        "prizes": [],
        "entrants": 0,
        "final": False,
        "standings": [],
    }
    assert [c["slug"] for c in listed] == ["pilot", "october-test"]
    assert listed[0]["prizes"] == [{"usd": 700, "skl": 100}, {"usd": 0, "skl": 100}]
    assert missing.status_code == 404


def test_the_list_carries_the_first_six_standings_of_each_page():
    conn = fresh_test_conn()
    with TestClient(app) as client:
        _save(TEST)
        for k in range(7):
            user_id, _, _ = TableWrite.create_user(conn, None, None, owner_workos_id=f"user_{k}")
            TableWrite.enter_competition(conn, "october-test", f"user_{k}", user_id, NOW, False)
        listed = client.get("/competitions").json()["competitions"]
        page = client.get("/competitions/october-test").json()
    conn.close()

    assert len(page["standings"]) == 7
    assert listed == [{**page, "standings": page["standings"][:6]}]


def test_an_owner_enters_and_leaves_with_only_their_own_agents(sign_in):
    accounts = AgentAccounts(fresh_test_db(), lambda *_: pytest.fail("must not onboard"))
    with TestClient(app) as client:
        _save(TEST)
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
                "runStartsAt": TEST.starts_at,
                "earned": "0",
                "returnPct": 0.0,
                "trades": 0,
                "disqualified": False,
            }
        ],
    )
    assert (keyed.status_code, foreign.status_code) == (403, 404)
    assert left.status_code == 204 and after[0]["competitions"] == []

