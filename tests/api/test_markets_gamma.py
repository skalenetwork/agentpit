import json

import pytest
from fastapi.testclient import TestClient

from agentpit.api.deps import get_db_session
from agentpit.api.main import app
from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.market_state import MarketState
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
        resolved = TableWrite.resolve_market(conn, market.market_id, 1)
    g = client.get(f"/markets/{market.market_id}").json()
    assert (g["winner"], g["resolvedAt"]) == ("No", resolved.resolved_at)
    assert resolved.resolved_at is not None


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
