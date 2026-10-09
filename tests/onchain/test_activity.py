"""GET /activity (§8.10) + SPLIT/REDEEM logging (missing-feature #01).
Public-by-address. Live-chain."""

import uuid

from tests.onchain._helpers import create_market, fresh_client, hdr, house, register


def _hdr(t):
    # One definition of the suite's credential lives in _helpers; six local
    # copies is how this file kept sending a bearer token after the cutover
    # made the API key the thing tests can actually mint.
    return hdr(t)


def _email():
    return f"e2e-{uuid.uuid4().hex[:8]}@example.com"


def test_activity_has_split_and_trade_rows(house_book):
    client = fresh_client()
    ra = register(client, _email())
    ta = ra["access_token"]
    a_addr = ra["user"]["eth_address"]

    market = create_market(client)
    mid = market["market_id"]
    yes = market["erc1155_tokens"][0][0]
    cond = market["condition_id"]["value"]
    house_book(yes, bids=(("0.6", "100"),), user=house(client))

    # A splits collateral → SPLIT activity row, then sells into the house → TRADE.
    client.post(f"/markets/{mid}/split_position", headers=_hdr(ta), json={"amount": 50_000_000})
    sold = client.post(
        "/order",
        headers=_hdr(ta),
        json={
            "token_id": yes,
            "side": "SELL",
            "price": "0.6",
            "size": 10,
            "order_type": "FAK",
        },
    ).json()
    assert sold["success"], sold

    acts = client.get(f"/activity?user={a_addr}").json()   # public-by-address, no auth
    types = {a["type"] for a in acts}
    assert "SPLIT" in types
    assert "TRADE" in types
    split = next(a for a in acts if a["type"] == "SPLIT")
    assert split["conditionId"] == cond
    assert split["size"] == 50.0
    assert isinstance(split["timestamp"], int) and split["timestamp"] > 0
    trade = next(a for a in acts if a["type"] == "TRADE")
    assert trade["asset"] == yes and isinstance(trade["price"], float)

    # type filter
    only_split = client.get(f"/activity?user={a_addr}&type=SPLIT").json()
    assert all(a["type"] == "SPLIT" for a in only_split) and only_split

    # unknown address → empty
    assert client.get("/activity?user=0x000000000000000000000000000000000000dEaD").json() == []
