"""Replaces deleted tests/api/test_lifecycle.py.

Markets state-machine: DRAFT → ACTIVE → CLOSED → RESOLVED, plus cancel and
invalid transitions. Lives on-chain because market creation does prepareCondition.
"""

from tests.onchain._helpers import ADMIN_HDR, create_market, fresh_client, hdr, register


def test_market_lifecycle_happy_path():
    client = fresh_client()
    market = create_market(client, active=False)
    mid = market["market_id"]
    assert market["market_state"] == "DRAFT"

    activate = client.post(f"/markets/{mid}/activate", headers=ADMIN_HDR).json()
    assert activate["market_state"] == "ACTIVE"
    # GET /markets/{id} returns the Gamma shape (no market_state string);
    # ACTIVE surfaces as active=true / acceptingOrders=true / closed=false.
    fetched = client.get(f"/markets/{mid}").json()
    assert fetched["active"] is True and fetched["acceptingOrders"] is True
    assert fetched["closed"] is False

    close = client.post(f"/markets/{mid}/close", headers=ADMIN_HDR).json()
    assert close["market_state"] == "CLOSED"
    # CLOSED surfaces in Gamma as closed=true / active=false.
    fetched = client.get(f"/markets/{mid}").json()
    assert fetched["closed"] is True and fetched["active"] is False
    order = {"token_id": market["erc1155_tokens"][0][0], "side": "BUY", "price": "0.5", "size": 10}
    refused = client.post("/order", headers=hdr(register(client)["api_key"]), json=order)
    assert refused.status_code == 400 and "is closed" in refused.json()["detail"]

    resolve = client.post(
        f"/markets/{mid}/resolve",
        json={"winning_outcome_index": 0},
        headers=ADMIN_HDR,
    ).json()
    assert resolve["market_state"] == "RESOLVED"
    assert resolve["payouts"] == [1, 0]


def test_cancel_market_from_draft():
    client = fresh_client()
    mid = create_market(client, active=False)["market_id"]
    cancel = client.post(f"/markets/{mid}/cancel", headers=ADMIN_HDR).json()
    assert cancel["market"]["market_state"] == "CANCELLED"
    # On-chain CTF positions: refund flows are now off-loaded to merge/redeem
    # by users themselves, so the backend-side counter is always 0.
    assert cancel["refunds_processed"] == 0
    # CANCELLED also surfaces as closed=true in the Gamma shape.
    assert client.get(f"/markets/{mid}").json()["closed"] is True


def test_invalid_state_transitions():
    client = fresh_client()
    mid = create_market(client, active=False)["market_id"]

    # DRAFT → CLOSE is invalid
    resp = client.post(f"/markets/{mid}/close", headers=ADMIN_HDR)
    assert resp.status_code == 400
    assert "ACTIVE" in resp.json()["detail"]

    client.post(f"/markets/{mid}/activate", headers=ADMIN_HDR)
    # ACTIVE → ACTIVATE is invalid
    resp = client.post(f"/markets/{mid}/activate", headers=ADMIN_HDR)
    assert resp.status_code == 400
    assert "DRAFT" in resp.json()["detail"]


def test_resolve_unknown_market_404():
    client = fresh_client()
    resp = client.post(
        "/markets/9999/resolve",
        json={"winning_outcome_index": 0},
        headers=ADMIN_HDR,
    )
    assert resp.status_code == 404


def test_resolve_twice_is_400():
    client = fresh_client()
    mid = create_market(client)["market_id"]
    client.post(
        f"/markets/{mid}/resolve",
        json={"winning_outcome_index": 0},
        headers=ADMIN_HDR,
    )
    resp = client.post(
        f"/markets/{mid}/resolve",
        json={"winning_outcome_index": 1},
        headers=ADMIN_HDR,
    )
    assert resp.status_code == 400
    assert "already resolved" in resp.json()["detail"]
