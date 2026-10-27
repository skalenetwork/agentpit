import json

import pytest
from fastapi.testclient import TestClient

from agentpit.api.deps import get_db_session
from agentpit.api.main import app
from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.market_state import MarketState
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite


def _hex32(seed: str) -> str:
    return "0x" + seed.encode().hex().ljust(64, "0")[:64]


@pytest.fixture()
def client_and_market():
    session = app.dependency_overrides[get_db_session]()
    req = CreateMarketRequest(
        question="Will it rain?",
        description="d",
        erc1155_tokens=[("111", "Yes"), ("222", "No")],
        slug="will-it-rain",
        condition_id=ConditionId(_hex32("c1")),
        state=MarketState.ACTIVE,
    )
    with session.write() as conn:
        market = TableWrite.create_market(conn, req, is_polygon_market=False)
    with TestClient(app) as client:
        yield client, market


def test_list_markets_returns_bare_gamma_array(client_and_market):
    client, market = client_and_market
    resp = client.get("/markets")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)  # bare array, no envelope
    g = next(m for m in body if m["id"] == str(market.market_id))
    assert g["conditionId"] == market.condition_id.value
    assert json.loads(g["outcomes"]) == ["Yes", "No"]
    assert json.loads(g["clobTokenIds"]) == ["111", "222"]
    assert g["active"] is True


def test_get_market_returns_single_gamma(client_and_market):
    client, market = client_and_market
    resp = client.get(f"/markets/{market.market_id}")
    assert resp.status_code == 200
    g = resp.json()
    assert g["id"] == str(market.market_id)
    assert g["conditionId"] == market.condition_id.value


def test_a_resolved_market_names_its_winner_and_when(client_and_market):
    client, market = client_and_market
    before = client.get(f"/markets/{market.market_id}").json()
    assert (before["winner"], before["resolvedAt"]) == (None, None)
    session = app.dependency_overrides[get_db_session]()
    with session.write() as conn:
        TableWrite.set_market_state(conn, market.market_id, MarketState.ACTIVE, MarketState.RESOLVED, (0, 1))
        resolved = TableRead.read_market(conn, market.market_id)
    assert resolved is not None and resolved.resolved_at is not None
    g = client.get(f"/markets/{market.market_id}").json()
    assert (g["winner"], g["resolvedAt"]) == ("No", resolved.resolved_at)
    assert g["outcomePrices"] == '["0","1"]'


def test_bridge_filter_by_condition_ids(client_and_market):
    client, market = client_and_market
    resp = client.get(f"/markets?condition_ids={market.condition_id.value}")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["id"] == str(market.market_id)


def test_filter_by_clob_token_ids(client_and_market):
    client, _ = client_and_market
    resp = client.get("/markets?clob_token_ids=222")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert json.loads(body[0]["clobTokenIds"]) == ["111", "222"]


def test_filter_by_id(client_and_market):
    client, market = client_and_market
    resp = client.get(f"/markets?id={market.market_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["id"] == str(market.market_id)


def test_filter_by_slug(client_and_market):
    client, market = client_and_market
    resp = client.get(f"/markets?slug={market.slug}")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["slug"] == market.slug


def test_id_filters_over_100_are_rejected(client_and_market):
    client, _ = client_and_market
    ids = ",".join(str(i) for i in range(101))
    assert client.get(f"/markets?clob_token_ids={ids}").status_code == 422
    assert client.get(f"/markets?condition_ids={ids}").status_code == 422


def test_id_filter_at_cap_still_matches(client_and_market):
    client, market = client_and_market
    ids = ",".join(["111"] + [str(9000 + i) for i in range(99)])
    resp = client.get(f"/markets?clob_token_ids={ids}")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["id"] == str(market.market_id)


def test_closed_and_active_filter_like_the_gamma_fields(client_and_market):
    client, market = client_and_market
    session = app.dependency_overrides[get_db_session]()
    with session.write() as conn:
        done = TableWrite.create_market(
            conn,
            CreateMarketRequest(
                question="Did it snow?",
                description="d",
                erc1155_tokens=[("333", "Yes"), ("444", "No")],
                slug="did-it-snow",
                condition_id=ConditionId(_hex32("c2")),
                state=MarketState.ACTIVE,
            ),
            is_polygon_market=False,
        )
        TableWrite.set_market_state(conn, done.market_id, MarketState.ACTIVE, MarketState.RESOLVED, (1, 0))

    def ids(query: str) -> set[str]:
        return {m["id"] for m in client.get(f"/markets?{query}").json()}

    assert ids("closed=false&active=true") == {str(market.market_id)}
    assert ids("closed=true") == ids("active=false") == {str(done.market_id)}
    assert ids("closed=false&active=false") == set()
