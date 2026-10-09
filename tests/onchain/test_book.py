"""GET /book serves Polymarket's book in OrderBookSummary shape (§8.5).
Live-chain (placing orders needs a prepared market)."""

from tests.onchain._helpers import create_market, fresh_client, register, hdr


def _yes(market) -> str:
    return market["erc1155_tokens"][0][0]


def test_book_is_polymarkets_book_and_never_shows_agent_orders(house_book):
    client = fresh_client()
    tok = register(client)["access_token"]
    market = create_market(client)
    yes = _yes(market)
    cond = market["condition_id"]["value"]
    house_book(yes, bids=(("0.40", "8"),))
    client.post(
        "/order",
        headers=hdr(tok),
        json={"token_id": yes, "side": "BUY", "price": "0.30", "size": 4},
    ).raise_for_status()

    body = client.get(f"/book?token_id={yes}").json()
    assert body["market"] == cond
    assert body["asset_id"] == yes
    assert body["tick_size"] == "0.001"
    assert body["neg_risk"] is False
    assert body["bids"] == [{"price": "0.4", "size": "8"}]
    assert body["asks"] == []
    assert body["timestamp"].isdigit()


def test_books_batch():
    client = fresh_client()
    tok = register(client)["access_token"]
    market = create_market(client)
    yes = _yes(market)
    client.post(
        "/order",
        headers=hdr(tok),
        json={"token_id": yes, "side": "BUY", "price": "0.40", "size": 5},
    )
    body = client.post("/books", json=[{"token_id": yes}]).json()
    assert isinstance(body, list) and len(body) == 1
    assert body[0]["asset_id"] == yes


def test_book_unknown_token_404():
    client = fresh_client()
    assert client.get("/book?token_id=999999").status_code == 404
