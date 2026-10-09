"""GET /midpoint, /price, /last-trade-price (§8.7). Live-chain: the house
book gives a deterministic book; 404 paths when book/trades are absent."""

from tests.onchain._helpers import create_market, fresh_client, register, hdr


def _yes(market) -> str:
    return market["erc1155_tokens"][0][0]


def test_midpoint_and_price_from_book(house_book):
    client = fresh_client()
    market = create_market(client)
    yes = _yes(market)
    house_book(yes, bids=(("0.40", "5"),), asks=(("0.60", "5"),))

    assert client.get(f"/midpoint?token_id={yes}").json() == {"mid": "0.5"}
    assert client.get(f"/price?token_id={yes}&side=BUY").json() == {"price": "0.6"}
    assert client.get(f"/price?token_id={yes}&side=SELL").json() == {"price": "0.4"}


def test_midpoint_404_without_book():
    client = fresh_client()
    tok = register(client)["access_token"]
    market = create_market(client)
    yes = _yes(market)
    client.post("/order", headers=hdr(tok), json={"token_id": yes, "side": "BUY", "price": "0.40", "size": 5})
    # No Polymarket book → no midpoint; no trades → last-trade-price 404.
    assert client.get(f"/midpoint?token_id={yes}").status_code == 404
    assert client.get(f"/last-trade-price?token_id={yes}").status_code == 404


def test_resting_orders_stay_off_the_book():
    client = fresh_client()
    tok = register(client)["access_token"]
    market = create_market(client)
    yes = _yes(market)

    r = client.post(
        "/order",
        headers=hdr(tok),
        json={"token_id": yes, "side": "BUY", "price": "0.40", "size": 5},
    )
    assert r.json()["status"] == "live", r.json()

    body = client.get(f"/book?token_id={yes}").json()
    assert (body["bids"], body["asks"]) == ([], [])
