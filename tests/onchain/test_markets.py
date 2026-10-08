"""Replaces deleted tests/api/test_markets.py.

Live-chain because POST /markets calls prepareCondition + registerToken.
"""

import json

from tests.onchain._helpers import create_market, fresh_client, unique_question


def test_create_and_get_market_round_trip():
    client = fresh_client()
    q = unique_question()
    created = create_market(client, q, state="DRAFT")
    assert created["market_state"] == "DRAFT"
    assert created["question"] == q
    assert created["condition_id"]["value"].startswith("0x")
    assert len(created["erc1155_tokens"]) == 2
    yes_id, yes_label = created["erc1155_tokens"][0]
    no_id, no_label = created["erc1155_tokens"][1]
    assert yes_label == "YES" and no_label == "NO"
    assert int(yes_id) > 0 and int(no_id) > 0

    # GET /markets/{id} returns the Gamma shape: numeric id as a string,
    # conditionId as a bare string, outcomes/clobTokenIds as JSON strings.
    fetched = client.get(f"/markets/{created['market_id']}").json()
    assert fetched["id"] == str(created["market_id"])
    assert fetched["conditionId"] == created["condition_id"]["value"]
    assert json.loads(fetched["outcomes"]) == [yes_label, no_label]
    assert json.loads(fetched["clobTokenIds"]) == [yes_id, no_id]


def test_get_unknown_market_returns_404():
    client = fresh_client()
    resp = client.get("/markets/9999")
    assert resp.status_code == 404


def test_list_markets_paginates():
    client = fresh_client()
    ids = []
    for _ in range(3):
        ids.append(create_market(client)["market_id"])

    # GET /markets returns a bare Gamma array; ids surface as strings.
    body = client.get("/markets?limit=100&offset=0").json()
    assert len(body) >= 3
    fetched_ids = {m["id"] for m in body}
    assert {str(i) for i in ids}.issubset(fetched_ids)

    page = client.get("/markets?limit=2&offset=0").json()
    assert len(page) == 2

    bad = client.get("/markets?limit=2000")
    assert bad.status_code == 400
    bad2 = client.get("/markets?offset=-1")
    assert bad2.status_code == 400
