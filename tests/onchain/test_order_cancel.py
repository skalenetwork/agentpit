"""Cancel-family semantics (§8.2): HTTP 200 always; results in
{canceled, not_canceled}. Live-chain because placing an order signs +
inserts against a market created via prepareCondition."""

from tests.onchain._helpers import create_market, fresh_client, register, hdr


def _place(client, token, *, token_id, side, price, size):
    return client.post(
        "/order",
        headers=hdr(token),
        json={"token_id": token_id, "side": side, "price": price, "size": size},
    ).json()


def _yes_token(market) -> str:
    return market["erc1155_tokens"][0][0]


def test_delete_order_cancels_and_is_idempotent():
    client = fresh_client()
    tok = register(client)["access_token"]
    market = create_market(client)
    yes = _yes_token(market)
    placed = _place(client, tok, token_id=yes, side="BUY", price="0.40", size=10)
    oid = placed["orderID"]

    r1 = client.request("DELETE", "/order", headers=hdr(tok), json={"orderID": oid})
    assert r1.status_code == 200
    assert r1.json() == {"canceled": [oid], "not_canceled": {}}

    # Second cancel: 200 with the id recorded in not_canceled (no 404).
    r2 = client.request("DELETE", "/order", headers=hdr(tok), json={"orderID": oid})
    assert r2.status_code == 200
    body = r2.json()
    assert body["canceled"] == []
    assert oid in body["not_canceled"]


def test_cancel_all_clears_my_live_orders():
    client = fresh_client()
    tok = register(client)["access_token"]
    market = create_market(client)
    yes = _yes_token(market)
    o1 = _place(client, tok, token_id=yes, side="BUY", price="0.30", size=5)["orderID"]
    o2 = _place(client, tok, token_id=yes, side="BUY", price="0.31", size=5)["orderID"]

    r = client.request("DELETE", "/cancel-all", headers=hdr(tok))
    assert r.status_code == 200
    assert set(r.json()["canceled"]) == {o1, o2}


def test_cancel_market_orders_filters_by_condition_id():
    client = fresh_client()
    tok = register(client)["access_token"]
    market = create_market(client)
    yes = _yes_token(market)
    cond = market["condition_id"]["value"]
    oid = _place(client, tok, token_id=yes, side="BUY", price="0.30", size=5)["orderID"]

    r = client.request(
        "DELETE", "/cancel-market-orders", headers=hdr(tok), json={"market": cond}
    )
    assert r.status_code == 200
    assert r.json()["canceled"] == [oid]


def test_delete_orders_bulk_and_dedup():
    client = fresh_client()
    tok = register(client)["access_token"]
    market = create_market(client)
    yes = _yes_token(market)
    o1 = _place(client, tok, token_id=yes, side="BUY", price="0.30", size=5)["orderID"]
    # Duplicate o1 + a missing id: dedup means o1 is cancelled once (not also
    # reported as not_canceled), and the missing id lands in not_canceled.
    r = client.request("DELETE", "/orders", headers=hdr(tok), json=[o1, o1, "0xmissing"])
    assert r.status_code == 200
    body = r.json()
    assert body["canceled"] == [o1]
    assert o1 not in body["not_canceled"]
    assert "0xmissing" in body["not_canceled"]


def test_delete_orders_caps_the_id_list():
    client = fresh_client()
    tok = register(client)["access_token"]
    ids = [f"0x{i:064x}" for i in range(1001)]
    r = client.request("DELETE", "/orders", headers=hdr(tok), json=ids)
    assert r.status_code == 422
    r = client.request("DELETE", "/orders", headers=hdr(tok), json=ids[:1000])
    assert r.status_code == 200
    assert r.json()["canceled"] == []
    assert len(r.json()["not_canceled"]) == 1000


def test_delete_orders_caps_each_id_at_100_characters():
    client = fresh_client()
    tok = register(client)["access_token"]
    r = client.request("DELETE", "/orders", headers=hdr(tok), json=["1" * 101])
    assert r.status_code == 422
    r = client.request("DELETE", "/orders", headers=hdr(tok), json=["1" * 100])
    assert r.status_code == 200
    assert "1" * 100 in r.json()["not_canceled"]


def test_delete_orders_never_cancels_another_users_order():
    client = fresh_client()
    owner = register(client)["access_token"]
    other = register(client)["access_token"]
    market = create_market(client)
    yes = _yes_token(market)
    oid = _place(client, owner, token_id=yes, side="BUY", price="0.30", size=5)["orderID"]
    r = client.request("DELETE", "/orders", headers=hdr(other), json=[oid])
    assert r.json()["canceled"] == []
    assert oid in r.json()["not_canceled"]
    r = client.request("DELETE", "/orders", headers=hdr(owner), json=[oid])
    assert r.json()["canceled"] == [oid]
