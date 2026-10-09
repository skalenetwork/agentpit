"""GET /data/trades — dual-perspective CLOB Trade[] (§8.4), secret-safe (§13)."""

import uuid

from fastapi.testclient import TestClient

from agentpit.api.app import create_app

from tests.onchain._helpers import (
    create_market,
    fresh_client,
    hdr,
    house,
    order_service,
    register,
)


def _hdr(t):
    # One definition of the suite's credential lives in _helpers; six local
    # copies is how this file kept sending a bearer token after the cutover
    # made the API key the thing tests can actually mint.
    return hdr(t)


def _email():
    return f"e2e-{uuid.uuid4().hex[:8]}@example.com"


def test_data_trades_dual_perspective_and_secret_safe(house_book):
    client = fresh_client()
    ra = register(client, _email())
    ta, a_uid = ra["access_token"], ra["user"]["user_id"]
    the_house = house(client)

    market = create_market(client)
    yes = market["erc1155_tokens"][0][0]
    cond = market["condition_id"]["value"]
    book = house_book(yes, asks=(("0.65", "100"),), user=the_house)

    # A rests a BUY YES @0.6; Polymarket's ask reaches it and the sweeper fills it.
    rested = client.post(
        "/order",
        headers=_hdr(ta),
        json={"token_id": yes, "side": "BUY", "price": "0.6", "size": 100},
    ).json()
    assert rested["status"] == "live", rested
    book.apply_price_change_entry(
        {"asset_id": book.asset_id, "side": "SELL", "price": "0.6", "size": "100"}
    )
    order_service(client).sweep()

    # Taker A's view.
    ra_trades = client.get("/data/trades", headers=_hdr(ta))
    abody = ra_trades.json()
    assert abody["next_cursor"] == "LTE="
    assert abody["count"] == 1
    t = abody["data"][0]
    assert t["trader_side"] == "TAKER"
    assert t["market"] == cond and t["asset_id"] == yes
    assert t["size"] == "100" and t["status"] == "MATCHED"
    assert t["owner"] == a_uid
    # The counterparty (the house) owner is its USER_ID, not an api_key.
    assert t["maker_orders"][0]["owner"] == the_house.user_id
    assert t["maker_orders"][0]["matched_amount"] == "100"

    # The house's (maker) view of the SAME fill.
    house_trades = client.get("/data/trades", headers=_hdr(the_house.api_key))
    hbody = house_trades.json()
    assert hbody["count"] == 1
    assert hbody["data"][0]["trader_side"] == "MAKER"
    assert hbody["data"][0]["owner"] == the_house.user_id

    # Secret-safety (§13): neither the api_key column names NOR the actual
    # api_key VALUES (incl. the counterparty's, surfaced cross-perspective)
    # ever appear in a response body.
    for raw in (ra_trades.text, house_trades.text):
        assert "TAKER_API_KEY" not in raw and "MAKER_API_KEY" not in raw
        assert "api_key" not in raw.lower()
        assert ta not in raw and the_house.api_key not in raw


def test_data_trades_empty_and_requires_auth():
    app = create_app()
    with TestClient(app) as client:
        body = register(client, _email())
        assert client.get("/data/trades", headers=_hdr(body["access_token"])).json() == {
            "limit": 100, "count": 0, "next_cursor": "LTE=", "data": []
        }
        assert client.get("/data/trades").status_code == 401
