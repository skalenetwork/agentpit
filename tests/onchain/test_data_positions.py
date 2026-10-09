"""GET /positions + /value (§8.8/8.9) — public-by-address. Live-chain."""

import uuid

from tests.onchain._helpers import create_market, fresh_client, hdr, house, register


def _hdr(t):
    # One definition of the suite's credential lives in _helpers; six local
    # copies is how this file kept sending a bearer token after the cutover
    # made the API key the thing tests can actually mint.
    return hdr(t)


def _email():
    return f"e2e-{uuid.uuid4().hex[:8]}@example.com"


def test_positions_and_value_public_by_address(house_book):
    client = fresh_client()
    ra = register(client, _email())
    ta = ra["access_token"]
    a_addr = ra["user"]["eth_address"]

    market = create_market(client)
    yes = market["erc1155_tokens"][0][0]
    cond = market["condition_id"]["value"]
    house_book(yes, asks=(("0.6", "100"),), user=house(client))

    # A buys 100 YES @0.6 from the house.
    bought = client.post(
        "/order",
        headers=_hdr(ta),
        json={
            "token_id": yes,
            "side": "BUY",
            "price": "0.6",
            "size": 100,
            "order_type": "FAK",
        },
    ).json()
    assert bought["success"], bought

    # Public-by-address: NO auth header.
    positions = client.get(f"/positions?user={a_addr}").json()
    assert isinstance(positions, list)
    yes_pos = [p for p in positions if p["asset"] == yes]
    assert len(yes_pos) == 1
    p = yes_pos[0]
    assert p["proxyWallet"] == a_addr
    assert p["conditionId"] == cond
    assert p["size"] == 100.0
    assert p["outcome"] == "YES" and p["outcomeIndex"] == 0
    assert abs(p["avgPrice"] - 0.6) < 1e-6
    assert isinstance(p["curPrice"], float)
    assert p["oppositeOutcome"] == "NO"

    value = client.get(f"/value?user={a_addr}").json()
    assert value == [{"user": a_addr, "value": p["currentValue"]}] or (
        len(value) == 1 and value[0]["user"] == a_addr
    )

    # Unknown address → empty.
    assert client.get("/positions?user=0x000000000000000000000000000000000000dEaD").json() == []
