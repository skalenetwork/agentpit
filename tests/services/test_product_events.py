import json
import time

import httpx
import pytest
from pydantic import ValidationError

from agentpit.datastructures.attribution import Attribution
from agentpit.db.table_write import TableWrite
from agentpit.services import product_events
from agentpit.services.agent_accounts import AgentAccounts
from agentpit.services.product_events import Forwarder, owner_hash
from tests.db_helpers import fresh_test_conn, fresh_test_db
from tests.services.test_competition_service import LOCKS, PRIZES, SLUG, _agent, _competition, _service

OWNER = "user_01OWNER"


def _events(conn, name: str | None = None) -> list[dict]:
    sql = "SELECT * FROM product_events" + (" WHERE NAME = %s" if name else "") + " ORDER BY ID"
    return conn.execute(sql, (name,) if name else ()).fetchall()


def _trade(conn, key: str, at: int, status: str = "CONFIRMED") -> None:
    conn.execute(
        "INSERT INTO trades (TRADE_ID, TAKER_ORDER_ID, MAKER_ORDERS, MARKET, ASSET_ID, PRICE, TRADE_SIZE, "
        "REMAINING_SIZE, SIDE, STATUS, MATCH_TIME, BUCKET_INDEX, FEE_RATE_BPS, TAKER_API_KEY, MAKER_API_KEY) "
        "VALUES (%s, 'o', '[]', 'm', 'a', 500000, 1000000, 0, 'BUY', %s, %s, 0, 0, %s, 'house')",
        (f"t-{key}-{at}", status, at, key),
    )


def test_track_records_once_in_a_lifetime_events_once():
    conn = fresh_test_conn()
    product_events.track(conn, "agent_created", owner=OWNER, agent="0xABC", runner="claude")
    product_events.track(conn, "agent_created", owner=OWNER, agent="0xabc", runner="claude")
    product_events.track(conn, "top_up", owner=OWNER, agent="0xabc", usd=100)
    product_events.track(conn, "top_up", owner=OWNER, agent="0xabc", usd=100)

    created = _events(conn, "agent_created")
    assert len(created) == 1
    assert created[0]["AGENT_ADDRESS"] == "0xabc"
    assert created[0]["PROPS"] == {"runner": "claude"}
    assert len(_events(conn, "top_up")) == 2


def test_a_failed_track_never_fails_the_callers_transaction():
    conn = fresh_test_conn()
    conn.autocommit = False
    product_events.track(conn, "top_up", owner=OWNER, bad=object())
    conn.execute("SELECT 1")
    conn.commit()
    conn.autocommit = True
    assert _events(conn, "top_up") == []


def test_first_trade_is_recorded_for_a_persons_agent_once_and_never_for_the_house():
    conn = fresh_test_conn()
    _, address, key = _agent(conn, owner=OWNER)
    _, _, house = _agent(conn, owner=None)

    product_events.first_trade(conn, key, "will-it-rain", 1_000)
    product_events.first_trade(conn, key, "another-market", 2_000)
    product_events.first_trade(conn, house, "will-it-rain", 1_000)

    [first] = _events(conn, "first_trade")
    assert (first["AGENT_ADDRESS"], first["AT"], first["PROPS"]["market"]) == (address.lower(), 1_000, "will-it-rain")
    assert first["OWNER_WORKOS_ID"] == OWNER


def test_backfill_adds_the_history_once_and_marks_agents_that_already_traded():
    conn = fresh_test_conn()
    TableWrite.ensure_owner(conn, OWNER, "console_code")
    traded, address, key = _agent(conn, owner=OWNER)
    _, idle, _ = _agent(conn, owner=OWNER)
    _trade(conn, key, 500, status="FAILED")
    _trade(conn, key, 700)
    _trade(conn, key, 900)

    product_events.backfill(conn)
    product_events.backfill(conn)

    assert [e["OWNER_WORKOS_ID"] for e in _events(conn, "owner_signed_up")] == [OWNER]
    assert {e["AGENT_ADDRESS"] for e in _events(conn, "agent_created")} == {address.lower(), idle.lower()}
    assert {e["PROPS"]["runner"] for e in _events(conn, "agent_created")} == {"api"}
    [first] = _events(conn, "first_trade")
    assert (first["AGENT_ADDRESS"], first["AT"]) == (address.lower(), 700)
    # A live fill after the backfill is not a first trade.
    product_events.first_trade(conn, key, "m", 1_000)
    assert len(_events(conn, "first_trade")) == 1


def test_backfill_never_brings_back_a_deleted_accounts_events():
    conn = fresh_test_conn()
    TableWrite.ensure_owner(conn, OWNER, "console_code")
    _, _, key = _agent(conn, owner=OWNER)
    _trade(conn, key, 700)
    product_events.backfill(conn)
    assert len(_events(conn)) == 3

    TableWrite.delete_account(conn, OWNER, None, int(time.time()))
    product_events.backfill(conn)
    product_events.first_trade(conn, key, "m", 1_000)

    assert _events(conn) == []


def test_ensure_owner_says_when_it_created_the_owner_and_first_touch_attribution_wins():
    conn = fresh_test_conn()
    assert TableWrite.ensure_owner(conn, OWNER, "mcp") is True
    assert TableWrite.ensure_owner(conn, OWNER, "console_code") is False

    TableWrite.record_attribution(conn, OWNER, Attribution(utm_source="x", utm_campaign="clash2"))
    TableWrite.record_attribution(conn, OWNER, Attribution(ref="reddit", utm_source="reddit", utm_medium="post"))

    row = conn.execute("SELECT * FROM owners WHERE WORKOS_ID = %s", (OWNER,)).fetchone()
    assert (row["SIGNUP_VIA"], row["FIRST_REF"], row["UTM_SOURCE"], row["UTM_MEDIUM"], row["UTM_CAMPAIGN"]) == (
        "mcp",
        "reddit",
        "x",
        "post",
        "clash2",
    )
    assert row["CREATED_AT"] is not None


def test_owner_created_records_the_sign_up_only_when_it_created_the_owner():
    conn = fresh_test_conn()
    assert product_events.owner_created(conn, OWNER, "console_redirect") is True
    assert product_events.owner_created(conn, OWNER, "mcp") is False
    [signed_up] = _events(conn, "owner_signed_up")
    assert signed_up["PROPS"] == {"via": "console_redirect"}


@pytest.mark.parametrize("value", ["has space", "a" * 41, "<script>", "ref/../x", "0x" + "g" * 40])
def test_attribution_refuses_anything_but_a_short_slug_or_an_address(value):
    with pytest.raises(ValidationError):
        Attribution(ref=value)


def test_attribution_takes_slugs_and_agent_addresses():
    assert Attribution(ref="0x" + "aB" * 20, utm_source="x.com", utm_campaign="kickoff-clash_2").empty() is False
    assert Attribution().empty() is True


def test_creating_an_agent_records_the_sign_up_and_the_agent():
    db = fresh_test_db()
    agent = AgentAccounts(db, lambda *_: None).create_api_agent(OWNER)
    with db.read() as conn:
        signed_up = _events(conn, "owner_signed_up")
        [created] = _events(conn, "agent_created")
    assert [e["PROPS"]["via"] for e in signed_up] == ["console"]
    assert created["AGENT_ADDRESS"] == agent.eth_address.lower()
    assert created["PROPS"] == {"runner": "api", "source": "api_key", "agents_before": 0}


def test_entering_and_leaving_a_competition_are_recorded():
    conn = fresh_test_conn()
    _competition(conn, prizes=PRIZES)
    user_id, address, _ = _agent(conn, owner="user_owner")
    service = _service()

    service.enroll(user_id, SLUG, LOCKS - 3_600, switch=False)
    service.enroll(user_id, SLUG, LOCKS - 3_000, switch=False)
    service.leave(user_id, SLUG, LOCKS - 2_000)
    service.leave(user_id, SLUG, LOCKS - 1_900)
    service.enroll(user_id, SLUG, LOCKS - 1_000, switch=False)

    entered = _events(conn, "competition_entered")
    assert [e["AT"] for e in entered] == [LOCKS - 3_600, LOCKS - 1_000]
    assert entered[0]["AGENT_ADDRESS"] == address.lower()
    assert entered[0]["PROPS"]["competition"] == SLUG
    assert entered[0]["PROPS"]["phase"] == "upcoming"
    assert entered[0]["PROPS"]["prizes"] is True
    [left] = _events(conn, "competition_left")
    assert (left["AT"], left["OWNER_WORKOS_ID"]) == (LOCKS - 2_000, "user_owner")
    # The backfill sees both entries already recorded.
    product_events.backfill(conn)
    assert len(_events(conn, "competition_entered")) == 2


def test_deleting_an_account_deletes_its_events():
    conn = fresh_test_conn()
    product_events.track(conn, "top_up", owner=OWNER, usd=1)
    product_events.track(conn, "top_up", owner="user_other", usd=1)
    TableWrite.delete_account(conn, OWNER, None, int(time.time()))
    assert [e["OWNER_WORKOS_ID"] for e in _events(conn)] == ["user_other"]


def _forwarder(db, handler) -> Forwarder:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return Forwarder(db, "phc_test", "https://eu.i.posthog.com", "s3cret", "test", client=client)


def test_the_forwarder_sends_hashed_ids_and_owner_attribution_and_marks_events_sent():
    db = fresh_test_db()
    now = int(time.time())
    with db.write() as conn:
        TableWrite.ensure_owner(conn, OWNER, "console_code")
        TableWrite.record_attribution(conn, OWNER, Attribution(utm_source="x"))
        product_events.track(conn, "competition_entered", owner=OWNER, agent="0xAbc", at=now, competition=SLUG)
        product_events.track(conn, "agent_created", owner=OWNER, agent="0xdef", at=now - 30 * 86_400)
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://eu.i.posthog.com/batch/"
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"status": 1})

    assert _forwarder(db, handler).flush() == 2

    live, historical = bodies
    assert "historical_migration" not in live and historical["historical_migration"] is True
    [event] = live["batch"]
    assert event["event"] == "competition_entered"
    assert event["distinct_id"] == owner_hash("s3cret", OWNER)
    props = event["properties"]
    assert props["agent_address"] == "0xabc"
    assert props["first_utm_source"] == "x" and props["signup_via"] == "console_code"
    assert props["$process_person_profile"] is False
    assert OWNER not in json.dumps(bodies)
    assert bodies[0]["api_key"] == "phc_test"
    with db.read() as conn:
        assert all(e["SENT_AT"] is not None for e in _events(conn))
    assert _forwarder(db, handler).flush() == 0


def test_a_failed_send_leaves_events_unsent_with_the_same_ids_for_the_retry():
    db = fresh_test_db()
    with db.write() as conn:
        product_events.track(conn, "top_up", owner=OWNER, usd=5)
    sent: list[str] = []

    def failing(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content)["batch"][0]["uuid"])
        return httpx.Response(503)

    with pytest.raises(httpx.HTTPStatusError):
        _forwarder(db, failing).flush()
    with pytest.raises(httpx.HTTPStatusError):
        _forwarder(db, failing).flush()
    assert sent[0] == sent[1]
    with db.read() as conn:
        assert _events(conn)[0]["SENT_AT"] is None
