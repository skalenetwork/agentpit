import json
import time

import httpx
import pytest
from pydantic import ValidationError

from agentpit.auth.workos_client import FakeWorkOsClient
from agentpit.datastructures.attribution import Attribution
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.services import product_events
from agentpit.services.agent_accounts import AgentAccounts
from agentpit.services.product_events import CAPTURE_URL, DAY, ERASE_AFTER_S, Forwarder, owner_hash
from tests.db_helpers import fresh_test_conn, fresh_test_db
from tests.services.test_authkit_service import _service as _auth_service
from tests.services.test_competition_service import LOCKS, PRIZES, SLUG, _agent, _competition, _service

OWNER = "user_01OWNER"
ERASE_URL = "https://eu.posthog.com/api/projects/7/persons/bulk_delete/"


def _events(conn, name: str | None = None) -> list[dict]:
    sql = "SELECT * FROM product_events" + (" WHERE NAME = %s" if name else "") + " ORDER BY ID"
    return conn.execute(sql, (name,) if name else ()).fetchall()


def _erasures(conn) -> list[str]:
    return [r["OWNER_WORKOS_ID"] for r in conn.execute("SELECT * FROM product_event_erasures").fetchall()]


def _trade(conn, key: str, at: int, status: str = "CONFIRMED") -> None:
    conn.execute(
        "INSERT INTO trades (TRADE_ID, TAKER_ORDER_ID, MAKER_ORDERS, MARKET, ASSET_ID, PRICE, TRADE_SIZE, "
        "REMAINING_SIZE, SIDE, STATUS, MATCH_TIME, BUCKET_INDEX, FEE_RATE_BPS, TAKER_API_KEY, MAKER_API_KEY) "
        "VALUES (%s, 'o', '[]', 'm', 'a', 500000, 1000000, 0, 'BUY', %s, %s, 0, 0, %s, 'house')",
        (f"t-{key}-{at}", status, at, key),
    )


def _person(conn, workos_id: str = OWNER) -> tuple[str, str, str]:
    user_id, account, key = TableWrite.create_user(conn, None, None)
    TableWrite.set_workos_user_id(conn, user_id, workos_id)
    TableWrite.ensure_owner(conn, workos_id, "console_code")
    return user_id, account.address, key


def _forwarder(db, handler) -> Forwarder:
    return Forwarder(
        db,
        project_token="phc_test",
        personal_api_key="phx_test",
        project_id="7",
        id_secret="s3cret",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _answer(request: httpx.Request, result: str = "ok") -> httpx.Response:
    batch = json.loads(request.content)["batch"]
    return httpx.Response(200, json={"results": {e["uuid"]: {"result": result, "details": "d"} for e in batch}})


def test_track_records_once_in_a_lifetime_events_once():
    conn = fresh_test_conn()
    product_events.track(conn, "agent_created", owner=OWNER, agent="0xABC", runner="claude")
    product_events.track(conn, "agent_created", owner=OWNER, agent="0xabc", runner="claude")
    product_events.track(conn, "top_up", owner=OWNER, agent="0xabc", usd=100)
    product_events.track(conn, "top_up", owner=OWNER, agent="0xabc", usd=100)

    [created] = _events(conn, "agent_created")
    assert created["AGENT_ADDRESS"] == "0xabc"
    assert created["PROPS"] == {"runner": "claude"}
    assert len(_events(conn, "top_up")) == 2


def test_a_failed_track_never_fails_the_callers_transaction():
    conn = fresh_test_conn()
    conn.autocommit = False
    product_events.track(conn, "top_up", owner=OWNER, bad=object())
    conn.execute("SELECT 1")
    conn.commit()
    conn.autocommit = True
    assert _events(conn, "top_up") == []


def test_derive_adds_each_milestone_once_at_its_original_time():
    conn = fresh_test_conn()
    TableWrite.ensure_owner(conn, OWNER, "console_code")
    _, address, key = _agent(conn, owner=OWNER)
    _, idle, _ = _agent(conn, owner=OWNER)
    _trade(conn, key, 500, status="FAILED")
    _trade(conn, key, 700)
    _trade(conn, key, 900)

    product_events.derive(conn)
    product_events.derive(conn)

    [signed_up] = _events(conn, "owner_signed_up")
    assert (signed_up["OWNER_WORKOS_ID"], signed_up["PROPS"]) == (OWNER, {"via": "console_code"})
    created = _events(conn, "agent_created")
    assert {e["AGENT_ADDRESS"] for e in created} == {address.lower(), idle.lower()}
    assert {e["PROPS"]["runner"] for e in created} == {"api"}
    assert sorted(e["PROPS"]["agents_before"] for e in created) == [0, 1]
    [first] = _events(conn, "first_trade")
    assert (first["AGENT_ADDRESS"], first["AT"], first["PROPS"]["market"]) == (address.lower(), 700, "m")


def test_first_trade_waits_for_an_earlier_fill_that_has_not_settled():
    conn = fresh_test_conn()
    TableWrite.ensure_owner(conn, OWNER, "mcp")
    _, _, key = _agent(conn, owner=OWNER)
    _trade(conn, key, 500, status="PENDING")
    _trade(conn, key, 700)

    product_events.derive(conn)
    assert _events(conn, "first_trade") == []

    TableWrite.settle_trades(conn, [f"t-{key}-500"], "FAILED", None)
    product_events.derive(conn)
    assert [e["AT"] for e in _events(conn, "first_trade")] == [700]


def test_a_person_account_that_trades_counts_under_its_owner():
    conn = fresh_test_conn()
    _, address, key = _person(conn)
    _trade(conn, key, 700)

    product_events.derive(conn)

    [first] = _events(conn, "first_trade")
    assert (first["OWNER_WORKOS_ID"], first["AGENT_ADDRESS"]) == (OWNER, address.lower())
    assert _events(conn, "agent_created") == []


def test_a_refused_sign_in_and_the_house_are_never_counted():
    conn = fresh_test_conn()
    TableWrite.ensure_owner(conn, "user_refused", "console_code")
    _, _, house = _agent(conn, owner=None)
    _trade(conn, house, 700)

    product_events.derive(conn)

    assert _events(conn) == []


def test_active_day_is_one_event_per_agent_per_utc_day():
    conn = fresh_test_conn()
    TableWrite.ensure_owner(conn, OWNER, "mcp")
    _, first, first_key = _agent(conn, owner=OWNER)
    _, second, second_key = _agent(conn, owner=OWNER)
    day = 20_000 * DAY
    for at in (day + 10, day + 20, day + DAY + 5):
        _trade(conn, first_key, at)
    _trade(conn, second_key, day + 30)
    _trade(conn, second_key, day + 40, status="FAILED")

    product_events.derive(conn)
    product_events.derive(conn)

    active = {(e["AGENT_ADDRESS"], e["AT"]) for e in _events(conn, "active_day")}
    assert active == {(first.lower(), day + 10), (first.lower(), day + DAY + 5), (second.lower(), day + 30)}


def test_derive_never_brings_back_a_deleted_accounts_events():
    conn = fresh_test_conn()
    _person(conn)
    _, _, key = _agent(conn, owner=OWNER)
    _trade(conn, key, 700)
    product_events.derive(conn)
    assert len(_events(conn)) == 4

    TableWrite.delete_account(conn, OWNER, None, int(time.time()))
    TableWrite.ensure_owner(conn, OWNER, "console_code")
    product_events.derive(conn)

    assert TableRead.get_owner(conn, OWNER) is None
    assert _events(conn) == []
    assert _erasures(conn) == [OWNER]


def test_an_account_with_no_events_queues_no_erasure():
    conn = fresh_test_conn()
    TableWrite.ensure_owner(conn, OWNER, "console_code")
    TableWrite.delete_account(conn, OWNER, None, int(time.time()))
    assert _erasures(conn) == []


def test_first_touch_attribution_wins():
    conn = fresh_test_conn()
    TableWrite.ensure_owner(conn, OWNER, "console_code")
    TableWrite.ensure_owner(conn, OWNER, "mcp")

    TableWrite.record_attribution(conn, OWNER, Attribution(utm_source="x", utm_campaign="clash2"))
    TableWrite.record_attribution(conn, OWNER, Attribution(ref="reddit", utm_source="reddit", utm_medium="post"))

    row = conn.execute("SELECT * FROM owners WHERE WORKOS_ID = %s", (OWNER,)).fetchone()
    assert (row["SIGNUP_VIA"], row["FIRST_REF"], row["UTM_SOURCE"], row["UTM_MEDIUM"], row["UTM_CAMPAIGN"]) == (
        "console_code",
        "reddit",
        "x",
        "post",
        "clash2",
    )
    assert row["CREATED_AT"] is not None


def test_an_owner_from_an_mcp_app_takes_no_tags_from_a_later_console_sign_in():
    workos = FakeWorkOsClient()
    svc, db = _auth_service(workos)
    identity = workos.create_user(email="mia@example.com", password_hash=None).workos_user_id
    AgentAccounts(db, lambda *_: pytest.fail("must not onboard")).agent_for(identity, "Claude", None, None)

    svc.send_code("mia@example.com")
    svc.sign_in("mia@example.com", workos.last_code("mia@example.com"), Attribution(utm_source="first"))
    svc.send_code("mia@example.com")
    svc.sign_in("mia@example.com", workos.last_code("mia@example.com"), Attribution(utm_medium="later"))

    with db.read() as conn:
        row = conn.execute("SELECT * FROM owners WHERE WORKOS_ID = %s", (identity,)).fetchone()
    assert (row["SIGNUP_VIA"], row["UTM_SOURCE"], row["UTM_MEDIUM"]) == ("mcp", None, None)


@pytest.mark.parametrize("value", ["has space", "a" * 41, "<script>", "ref/../x", "0x" + "g" * 40])
def test_attribution_refuses_anything_but_a_short_slug_or_an_address(value):
    with pytest.raises(ValidationError):
        Attribution(ref=value)


def test_attribution_takes_slugs_and_agent_addresses():
    tags = Attribution(ref="0x" + "aB" * 20, utm_source="x.com", utm_campaign="kickoff-clash_2")
    assert tags.utm_campaign == "kickoff-clash_2"


def test_a_top_up_needs_a_live_owner():
    conn = fresh_test_conn()
    user_id, address, _ = _person(conn)
    product_events.top_up(conn, user_id, 1_000, 500)
    [event] = _events(conn, "top_up")
    assert (event["OWNER_WORKOS_ID"], event["AGENT_ADDRESS"], event["PROPS"]) == (OWNER, address.lower(), {"usd": 500})

    TableWrite.delete_account(conn, OWNER, None, int(time.time()))
    product_events.top_up(conn, user_id, 2_000, 500)
    assert _events(conn, "top_up") == []


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
    product_events.derive(conn)
    assert len(_events(conn, "competition_entered")) == 2


def test_switching_agents_records_the_replaced_agent_leaving():
    conn = fresh_test_conn()
    _competition(conn, prizes=PRIZES)
    replaced, replaced_address, _ = _agent(conn, owner="user_owner")
    chosen, _, _ = _agent(conn, owner="user_owner")
    service = _service()

    service.enroll(replaced, SLUG, LOCKS - 3_600, switch=False)
    service.enroll(chosen, SLUG, LOCKS - 3_000, switch=True)

    [left] = _events(conn, "competition_left")
    assert (left["AGENT_ADDRESS"], left["AT"]) == (replaced_address.lower(), LOCKS - 3_000)
    assert left["PROPS"] == {"competition": SLUG, "switched": True}


def test_a_relinked_identity_takes_its_events_and_the_old_id_is_erased():
    workos = FakeWorkOsClient()
    svc, db = _auth_service(workos)
    svc.send_code("erin@example.com")
    first = svc.sign_in("erin@example.com", workos.last_code("erin@example.com"))
    gone = first.user.workos_user_id
    with db.write() as conn:
        product_events.derive(conn)
    assert _forwarder(db, _answer).flush() == 1
    workos.delete_user(gone)

    svc.send_code("erin@example.com")
    second = svc.sign_in("erin@example.com", workos.last_code("erin@example.com"))

    with db.read() as conn:
        [signed_up] = _events(conn, "owner_signed_up")
        assert signed_up["OWNER_WORKOS_ID"] == second.user.workos_user_id
        assert signed_up["SENT_AT"] is None
        assert _erasures(conn) == [gone]


def test_the_forwarder_sends_hashed_ids_and_person_properties_and_marks_events_sent():
    db = fresh_test_db()
    now = int(time.time())
    with db.write() as conn:
        TableWrite.ensure_owner(conn, OWNER, "console_code")
        TableWrite.record_attribution(conn, OWNER, Attribution(utm_source="x"))
        product_events.track(conn, "competition_entered", owner=OWNER, agent="0xAbc", at=now, competition=SLUG)
        product_events.track(conn, "agent_created", owner=OWNER, agent="0xdef", at=now - 30 * DAY)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _answer(request)

    assert _forwarder(db, handler).flush() == 2

    live, historical = [json.loads(r.content) for r in requests]
    assert str(requests[0].url) == CAPTURE_URL
    assert requests[0].headers["authorization"] == "Bearer phc_test"
    assert requests[0].headers["posthog-attempt"] == "1"
    assert "historical_migration" not in live and historical["historical_migration"] is True
    [event] = live["batch"]
    assert event["event"] == "competition_entered"
    assert event["distinct_id"] == owner_hash("s3cret", OWNER)
    assert event["timestamp"].endswith(".000Z")
    properties = event["properties"]
    assert properties["agent_address"] == "0xabc"
    assert properties["first_utm_source"] == "x" and properties["signup_via"] == "console_code"
    assert properties["$set_once"] == {"signup_via": "console_code", "first_utm_source": "x"}
    assert properties["$geoip_disable"] is True
    assert OWNER not in json.dumps([live, historical])
    with db.read() as conn:
        assert all(e["SENT_AT"] is not None for e in _events(conn))
    assert _forwarder(db, handler).flush() == 0


@pytest.mark.parametrize(("result", "settled"), [("ok", True), ("warning", True), ("drop", True), ("retry", False)])
def test_only_a_retry_result_keeps_an_event_queued(result, settled):
    db = fresh_test_db()
    with db.write() as conn:
        product_events.track(conn, "top_up", owner=OWNER, usd=5)

    _forwarder(db, lambda request: _answer(request, result)).flush()

    with db.read() as conn:
        assert (_events(conn)[0]["SENT_AT"] is not None) is settled


def test_a_failed_send_leaves_events_unsent_with_the_same_ids_for_the_retry():
    db = fresh_test_db()
    with db.write() as conn:
        product_events.track(conn, "top_up", owner=OWNER, usd=5)
    sent: list[tuple[str, str]] = []

    def failing(request: httpx.Request) -> httpx.Response:
        sent.append((json.loads(request.content)["batch"][0]["uuid"], request.headers["posthog-request-id"]))
        return httpx.Response(503)

    with pytest.raises(httpx.HTTPStatusError):
        _forwarder(db, failing).flush()
    with pytest.raises(httpx.HTTPStatusError):
        _forwarder(db, failing).flush(attempt=2)
    assert sent[0] == sent[1]
    with db.read() as conn:
        assert _events(conn)[0]["SENT_AT"] is None


def test_erasure_waits_for_batches_in_flight_then_clears_the_queue():
    db = fresh_test_db()
    with db.write() as conn:
        product_events.track(conn, "top_up", owner=OWNER, usd=5)
        TableWrite.delete_account(conn, OWNER, None, int(time.time()))
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(202, json={"persons_found": 1, "deletion_errors": []})

    _forwarder(db, handler).erase()
    assert requests == []

    with db.write() as conn:
        conn.execute("UPDATE product_event_erasures SET AT = AT - %s", (ERASE_AFTER_S + 1,))
    _forwarder(db, handler).erase()

    [request] = requests
    assert str(request.url) == ERASE_URL
    assert request.headers["authorization"] == "Bearer phx_test"
    assert json.loads(request.content) == {"distinct_ids": [owner_hash("s3cret", OWNER)], "delete_events": True}
    with db.read() as conn:
        assert _erasures(conn) == []


def test_an_erasure_posthog_could_not_finish_stays_queued():
    db = fresh_test_db()
    with db.write() as conn:
        product_events.track(conn, "top_up", owner=OWNER, usd=5)
        TableWrite.delete_account(conn, OWNER, None, int(time.time()) - ERASE_AFTER_S - 1)

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"deletion_errors": [{"person_uuid": "p", "step": "delete"}]})

    with pytest.raises(RuntimeError):
        _forwarder(db, handler).erase()
    with db.read() as conn:
        assert _erasures(conn) == [OWNER]
