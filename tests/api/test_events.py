"""HTTP-level tests for the /events endpoints."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agentpit.api.deps import get_db_session, get_settings
from agentpit.api.main import app as _app
from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.market_state import MarketState
from agentpit.db.table_write import TableWrite


@pytest.fixture()
def client_and_db():
    """A TestClient plus the live DB session it routes to."""
    session = _app.dependency_overrides[get_db_session]()
    with TestClient(_app) as client:
        yield client, session


def _hex32(seed: str) -> str:
    return "0x" + seed.encode().hex().ljust(64, "0")[:64]


def _seed_market(
    session: Any,
    *,
    question: str,
    cond_id: str,
    event_id: int | None = None,
    outcome_label: str | None = None,
):
    req = CreateMarketRequest(
        question=question,
        description=f"d-{question}",
        erc1155_tokens=[(f"{cond_id}-y", "Yes"), (f"{cond_id}-n", "No")],
        slug=question.lower().replace(" ", "-").replace("?", ""),
        condition_id=ConditionId(cond_id),
        state=MarketState.ACTIVE,
        event_id=event_id,
        outcome_label=outcome_label,
    )
    with session.write() as conn:
        return TableWrite.create_market(conn, req, is_polygon_market=False)


# ----- GET /events ------------------------------------------------------------


def test_list_events_returns_empty_when_no_events(client_and_db):
    client, _ = client_and_db
    resp = client.get("/events?limit=10&offset=0")
    assert resp.status_code == 200
    body = resp.json()
    assert body == []


def test_list_events_returns_events_with_their_markets(client_and_db):
    client, session = client_and_db
    with session.write() as conn:
        event = TableWrite.upsert_event(conn, slug="wc", title="WC")
    m1 = _seed_market(
        session,
        question="france?",
        cond_id=_hex32("fr"),
        event_id=event.event_id,
        outcome_label="France",
    )
    m2 = _seed_market(
        session,
        question="spain?",
        cond_id=_hex32("es"),
        event_id=event.event_id,
        outcome_label="Spain",
    )

    resp = client.get("/events?limit=10&offset=0")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 1
    ev = body[0]
    assert ev["slug"] == "wc"
    market_ids = {int(m["id"]) for m in ev["markets"]}
    assert market_ids == {m1.market_id, m2.market_id}


def test_list_events_rejects_bad_pagination(client_and_db):
    client, _ = client_and_db
    resp = client.get("/events?limit=0")
    assert resp.status_code == 400


def test_list_events_can_filter_by_category(client_and_db):
    # Politics rather than Sports: Sports is in `Settings.excluded_categories`
    # by default, so using it here would test the exclusion, not the filter.
    client, session = client_and_db
    with session.write() as conn:
        politics = TableWrite.upsert_event(
            conn, slug="election", title="Election", category="Politics"
        )
        TableWrite.upsert_event(conn, slug="btc", title="BTC", category="Crypto")
    _seed_market(
        session,
        question="candidate-a?",
        cond_id=_hex32("ca"),
        event_id=politics.event_id,
        outcome_label="Candidate A",
    )

    resp = client.get("/events?limit=10&offset=0&category=Politics")
    assert resp.status_code == 200
    body = resp.json()
    # Gamma shape: a bare list, no {events, total} envelope.
    assert isinstance(body, list)
    assert [e["slug"] for e in body] == ["election"]
    assert body[0]["category"] == "Politics"


def test_list_events_category_filter_is_case_insensitive(client_and_db):
    """The UI sends its own canonical label; a case drift must not silently
    return zero events."""
    client, session = client_and_db
    with session.write() as conn:
        TableWrite.upsert_event(
            conn, slug="election", title="Election", category="Politics"
        )
        TableWrite.upsert_event(conn, slug="btc", title="BTC", category="Crypto")

    resp = client.get("/events?limit=10&offset=0&category=pOlItIcS")
    assert resp.status_code == 200
    assert [e["slug"] for e in resp.json()] == ["election"]


def test_list_events_blank_category_is_treated_as_no_filter(client_and_db):
    client, session = client_and_db
    with session.write() as conn:
        TableWrite.upsert_event(
            conn, slug="election", title="Election", category="Politics"
        )
        TableWrite.upsert_event(conn, slug="btc", title="BTC", category="Crypto")

    resp = client.get("/events?limit=10&offset=0&category=%20")
    assert resp.status_code == 200
    assert {e["slug"] for e in resp.json()} == {"election", "btc"}


def test_list_event_categories_returns_distinct_categories(client_and_db):
    client, session = client_and_db
    with session.write() as conn:
        TableWrite.upsert_event(
            conn, slug="election", title="Election", category="Politics"
        )
        TableWrite.upsert_event(conn, slug="btc", title="BTC", category="Crypto")
        TableWrite.upsert_event(conn, slug="btc2", title="BTC 2", category="Crypto")

    resp = client.get("/events/categories")
    assert resp.status_code == 200
    assert resp.json() == {"categories": ["Crypto", "Politics"]}


# ----- categories the product does not carry ---------------------------------


@pytest.fixture()
def sports_excluded():
    settings = _app.dependency_overrides[get_settings]
    excluded = settings().model_copy(update={"excluded_categories": ["Sports"]})
    _app.dependency_overrides[get_settings] = lambda: excluded
    yield
    _app.dependency_overrides[get_settings] = settings


def test_an_excluded_category_is_absent_from_the_grid(sports_excluded, client_and_db):
    client, session = client_and_db
    with session.write() as conn:
        TableWrite.upsert_event(conn, slug="cs2", title="CS2 match", category="Sports")
        TableWrite.upsert_event(conn, slug="btc", title="BTC", category="Crypto")

    resp = client.get("/events?limit=10&offset=0")
    assert resp.status_code == 200
    assert [e["slug"] for e in resp.json()] == ["btc"]


def test_an_excluded_category_gets_no_tab_to_click(sports_excluded, client_and_db):
    """The tab list and the grid come from different queries; a Sports tab over
    a grid that refuses to show Sports is a dead click."""
    client, session = client_and_db
    with session.write() as conn:
        TableWrite.upsert_event(conn, slug="cs2", title="CS2 match", category="Sports")
        TableWrite.upsert_event(conn, slug="btc", title="BTC", category="Crypto")

    assert client.get("/events/categories").json() == {"categories": ["Crypto"]}


def test_asking_for_an_excluded_category_returns_nothing(
    sports_excluded, client_and_db
):
    """A bookmarked ?category=Sports must not be a back door into the rows."""
    client, session = client_and_db
    with session.write() as conn:
        TableWrite.upsert_event(conn, slug="cs2", title="CS2 match", category="Sports")

    resp = client.get("/events?limit=10&offset=0&category=Sports")
    assert resp.status_code == 200
    assert resp.json() == []


# ----- GET /events/{slug} -----------------------------------------------------


def test_get_event_by_slug_returns_404_when_missing(client_and_db):
    client, _ = client_and_db
    resp = client.get("/events/nope")
    assert resp.status_code == 404


def test_get_event_by_slug_returns_event_and_markets(client_and_db):
    client, session = client_and_db
    with session.write() as conn:
        event = TableWrite.upsert_event(
            conn,
            slug="wc",
            title="2026 FIFA World Cup Winner",
            description="Who lifts the cup?",
            icon_url="https://i/wc.svg",
            category="Sports",
        )
    m1 = _seed_market(
        session,
        question="france?",
        cond_id=_hex32("fr"),
        event_id=event.event_id,
        outcome_label="France",
    )

    resp = client.get("/events/wc")
    assert resp.status_code == 200
    ev = resp.json()
    assert ev["slug"] == "wc"
    assert ev["title"] == "2026 FIFA World Cup Winner"
    assert ev["icon"] == "https://i/wc.svg"
    assert len(ev["markets"]) == 1
    assert int(ev["markets"][0]["id"]) == m1.market_id
    assert json.loads(ev["markets"][0]["outcomes"]) == ["Yes", "No"]
