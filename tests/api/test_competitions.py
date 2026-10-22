import struct
import time
from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from agentpit.api import og_card
from agentpit.api.main import app
from agentpit.datastructures.competition import HOUR, Competition, Prize, Race, Series, Standing
from agentpit.domain.runner import runner_for
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.services.agent_accounts import AgentAccounts
from agentpit.services.competition_service import invalidate
from tests.db_helpers import fresh_test_conn, fresh_test_db

DAY = 86_400
NOW = int(time.time())
TEST = Competition(slug="october-test", title="October test", starts_at=NOW + 2 * DAY, ends_at=NOW + 5 * DAY, prizes=[])


def _save(*competitions: Competition) -> None:
    conn = fresh_test_conn()
    for competition in competitions:
        TableWrite.save_competition(conn, competition, NOW)
    conn.close()


def _entrant(conn, slug: str, owner: str) -> tuple[str, str]:
    user_id, account, _ = TableWrite.create_user(conn, None, None, owner_workos_id=owner)
    TableWrite.enter_competition(conn, slug, owner, user_id, NOW - 9 * DAY, False)
    return user_id, account.address


def test_competitions_are_public_with_their_prizes():
    pilot = Competition(
        slug="pilot", title="Pilot", starts_at=NOW + 3 * DAY, ends_at=NOW + 5 * DAY, prizes=[Prize(usd=700, skl=100), Prize(usd=0, skl=100)]
    )
    with TestClient(app) as client:
        _save(TEST, pilot)
        listed = client.get("/competitions").json()["competitions"]
        page = client.get("/competitions/october-test").json()
        missing = client.get("/competitions/nope")

    starts = datetime.fromtimestamp(TEST.starts_at, UTC)
    assert NOW <= page.pop("builtAt") <= time.time()
    assert page == {
        "slug": "october-test",
        "title": "October test",
        "startsAt": TEST.starts_at,
        "endsAt": TEST.ends_at,
        "phase": "upcoming",
        "final": False,
        "prizes": [],
        "entrants": 0,
        "ranked": 0,
        "line": f"October test starts {starts:%b} {starts.day} at {starts:%H:%M} UTC with 0 agents entered",
        "leadChanges": 0,
        "standings": [],
        "series": {"at": [], "lines": []},
        "fixtures": [],
        "tape": [],
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

    race = {key: page[key] for key in page.keys() - {"builtAt", "leadChanges", "series", "fixtures", "tape"}}
    assert len(page["standings"]) == 7
    assert listed == [{**race, "standings": page["standings"][:6]}]


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
                "address": mine.eth_address,
                "name": mine.handle,
                "runner": {"slug": "claude", "label": "Claude", "host": None},
                "listed": False,
                "runStartsAt": TEST.starts_at,
                "endAt": None,
                "trades": 0,
                "lastTradeAt": None,
                "disqualified": False,
                "rank": None,
                "earned": "0",
                "returnPct": 0.0,
                "rankChange": None,
                "behind": None,
                "wouldRank": None,
                "hoursLed": 0,
                "bids": None,
                "bell": None,
            }
        ],
    )
    assert (keyed.status_code, foreign.status_code) == (403, 404)
    assert left.status_code == 204 and after[0]["competitions"] == []


def test_an_agent_query_swaps_the_line_and_adds_the_agent_s_chart_line():
    conn = fresh_test_conn()
    with TestClient(app) as client:
        _save(TEST)
        _, address = _entrant(conn, "october-test", "user_a")
        plain = client.get("/competitions/october-test").json()
        mine = client.get(f"/competitions/october-test?agent={address.lower()}").json()
        stranger = client.get(f"/competitions/october-test?agent=0x{'ab' * 20}").json()
    conn.close()

    starts = datetime.fromtimestamp(TEST.starts_at, UTC)
    assert mine["line"] == f"{plain['standings'][0]['name']} is entered in October test, starting {starts:%b} {starts.day} at {starts:%H:%M} UTC"
    assert mine["series"] == {"at": [], "lines": [{"address": address, "ranks": [None]}]}
    assert {**mine, "line": plain["line"], "series": plain["series"]} == plain
    assert stranger == plain


def test_an_agent_lists_its_entries_until_a_week_after_their_end():
    conn = fresh_test_conn()
    with TestClient(app) as client:
        for slug, ends in (("old", NOW - 8 * DAY), ("recent", NOW - 6 * DAY), ("live", NOW + DAY), ("other", NOW + DAY)):
            _save(Competition(slug=slug, title=slug.title(), starts_at=ends - DAY, ends_at=ends, prizes=[]))
        user_id, address = _entrant(conn, "old", "user_a")
        for slug in ("recent", "live"):
            TableWrite.enter_competition(conn, slug, "user_a", user_id, NOW - 9 * DAY, False)
        _entrant(conn, "other", "user_b")
        entries = client.get(f"/agents/{address.upper().replace('0X', '0x')}/competitions").json()["entries"]
        stranger = client.get(f"/agents/0x{'ab' * 20}/competitions").json()
    conn.close()

    assert [(e["race"]["slug"], e["standing"]["address"]) for e in entries] == [("live", address), ("recent", address)]
    assert set(entries[1]) == {"race", "standing", "above", "below", "line"}
    assert entries[1]["line"] == f"{entries[1]['standing']['name']} did not place in Recent"
    assert stranger == {"entries": []}


def test_the_race_card_is_drawn_once_an_hour_and_its_final_card_only_once_final(monkeypatch):
    renders: list[int] = []
    real = og_card.render_race_card

    def render(*args):
        renders.append(1)
        return real(*args)

    monkeypatch.setattr(og_card, "render_race_card", render)
    with TestClient(app) as client:
        _save(Competition(slug="done", title="Done", starts_at=NOW - 3 * DAY, ends_at=NOW - DAY, prizes=[Prize(usd=700, skl=100)]))
        hour = client.get("/competitions/done/card.png")
        again = client.get("/competitions/done/card.png")
        early = client.get("/competitions/done/card.png?final=1")
        conn = fresh_test_conn()
        conn.execute("UPDATE competitions SET FINAL_AT = %s", (NOW,))
        conn.close()
        invalidate()
        final = client.get("/competitions/done/card.png?final=1")
        unknown = client.get("/competitions/nope/card.png")

    assert (hour.status_code, hour.headers["content-type"], hour.headers["cache-control"]) == (200, "image/png", "public, max-age=300")
    assert struct.unpack(">II", hour.content[16:24]) == (1200, 630)
    assert again.content == hour.content
    assert (early.status_code, early.content, unknown.status_code) == (404, b"", 404)
    assert (final.status_code, final.headers["cache-control"]) == (200, "public, max-age=2592000")
    assert len(renders) == 2


def test_the_race_card_draws_the_robots_of_its_first_eight_entrants_until_one_places(monkeypatch):
    requested: list[str] = []

    def serve(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, content=b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 120"/>')

    monkeypatch.setattr(og_card, "_http", httpx.Client(transport=httpx.MockTransport(serve)))
    conn = fresh_test_conn()
    with TestClient(app) as client:
        _save(TEST)
        robots = {f"https://agentpit.dev/agents/{_entrant(conn, 'october-test', f'user_{k}')[1].lower()}/avatar.svg" for k in range(9)}
        resp = client.get("/competitions/october-test/card.png")
    conn.close()

    assert resp.status_code == 200
    assert len(requested) == len(set(requested) & robots) == 8


def _standing(rank: int) -> Standing:
    return Standing(
        address=f"0x{rank:040x}",
        name="FleetIsland" if rank == 1 else f"Agent {rank}",
        runner=runner_for(None, None),
        listed=True,
        runStartsAt=NOW,
        endAt=None,
        trades=3,
        lastTradeAt=NOW,
        disqualified=False,
        rank=rank,
        earned=0,
        returnPct=1.5 - rank,
        rankChange=None,
        behind=None if rank == 1 else 41_200_000,
        wouldRank=None,
        hoursLed=0,
        bids=None,
        bell=None,
    )


def test_the_race_card_draws_a_podium_in_every_phase():
    standings = [_standing(rank) for rank in (1, 2, 3, 4)]
    live = Race(
        slug="cup",
        title="Halloween cup",
        startsAt=NOW - DAY,
        endsAt=NOW + DAY,
        phase="live",
        final=False,
        prizes=[Prize(usd=500, skl=0)],
        entrants=4,
        ranked=4,
        line="",
        builtAt=NOW,
        leadChanges=0,
        standings=standings,
        series=Series(at=[], lines=[]),
        fixtures=[],
        tape=[],
    )
    robots = {s.address: b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 120"/>' for s in standings}
    for race in (
        live,
        live.model_copy(update={"ranked": 1}),
        live.model_copy(update={"phase": "ended"}),
        live.model_copy(update={"phase": "ended", "final": True}),
    ):
        assert struct.unpack(">II", og_card.render_race_card(race, NOW - NOW % HOUR, robots)[16:24]) == (1200, 630)
