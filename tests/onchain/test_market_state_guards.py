from tests.db_helpers import fresh_test_db
from tests.onchain._helpers import ADMIN_HDR, create_market, fresh_client, hdr, register


def _order_rows(key: str) -> int:
    with fresh_test_db().read() as conn:
        return len(conn.execute("SELECT ORDER_ID FROM orders WHERE API_KEY = %s", (key,)).fetchall())


def test_order_on_a_draft_market_is_refused():
    client = fresh_client()
    key = register(client)["api_key"]
    market = create_market(client, state="DRAFT")
    yes = market["erc1155_tokens"][0][0]
    r = client.post("/order", headers=hdr(key), json={"token_id": yes, "side": "BUY", "price": "0.5", "size": 10})
    assert r.status_code == 400
    assert "not open for trading" in r.json()["detail"]
    assert _order_rows(key) == 0


def test_order_on_a_closed_market_is_refused():
    client = fresh_client()
    key = register(client)["api_key"]
    market = create_market(client)
    client.post(f"/markets/{market['market_id']}/close", headers=ADMIN_HDR).raise_for_status()
    yes = market["erc1155_tokens"][0][0]
    r = client.post("/order", headers=hdr(key), json={"token_id": yes, "side": "BUY", "price": "0.5", "size": 10})
    assert r.status_code == 400
    assert "not open for trading" in r.json()["detail"]


def test_split_is_refused_once_resolved_but_merge_still_works():
    client = fresh_client()
    key = register(client)["api_key"]
    market = create_market(client)
    mid = market["market_id"]
    client.post(f"/markets/{mid}/split_position", headers=hdr(key), json={"amount": 10_000_000}).raise_for_status()
    client.post(f"/markets/{mid}/close", headers=ADMIN_HDR).raise_for_status()
    client.post(f"/markets/{mid}/resolve", json={"winning_outcome_index": 0}, headers=ADMIN_HDR).raise_for_status()
    split = client.post(f"/markets/{mid}/split_position", headers=hdr(key), json={"amount": 1_000_000})
    merge = client.post(f"/markets/{mid}/merge_positions", headers=hdr(key), json={"amount": 1_000_000})
    assert split.status_code == 400 and "ACTIVE" in split.json()["detail"]
    assert merge.status_code == 200, merge.text  # merge is user-paid and the only way back from YES+NO


def test_merge_still_works_on_a_cancelled_market_but_split_does_not():
    client = fresh_client()
    key = register(client)["api_key"]
    market = create_market(client)
    mid = market["market_id"]
    yes_id, no_id = market["erc1155_tokens"][0][0], market["erc1155_tokens"][1][0]
    client.post(f"/markets/{mid}/split_position", headers=hdr(key), json={"amount": 10_000_000}).raise_for_status()
    cancel = client.post(f"/markets/{mid}/cancel", headers=ADMIN_HDR)
    cancel.raise_for_status()
    assert cancel.json()["market"]["market_state"] == "CANCELLED"   # the guard is tested against the state it names

    split = client.post(f"/markets/{mid}/split_position", headers=hdr(key), json={"amount": 1_000_000})
    assert split.status_code == 400 and "ACTIVE" in split.json()["detail"]
    before = int(client.get("/balance-allowance", headers=hdr(key)).json()["balance"])

    merge = client.post(f"/markets/{mid}/merge_positions", headers=hdr(key), json={"amount": 1_000_000})
    assert merge.status_code == 200, merge.text  # a cancelled market's holders get their collateral back this way
    body = merge.json()
    assert body["amount"] == 1_000_000
    assert body["token_balances"][yes_id] == 9_000_000 and body["token_balances"][no_id] == 9_000_000
    # The point of allowing it: the merged pair came back as apUSD.
    after = int(client.get("/balance-allowance", headers=hdr(key)).json()["balance"])
    assert after == before + 1_000_000
