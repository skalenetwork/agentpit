"""trades ledger owner-attribution (§7): MARKET=condition_id, api_key columns
populated, MAKER_ORDERS carries USER_ID owner + maker_address."""

import json
import uuid

from tests.db_helpers import fresh_test_db
from tests.onchain._helpers import create_market, fresh_client, hdr, house, register


def _hdr(t):
    # One definition of the suite's credential lives in _helpers; six local
    # copies is how this file kept sending a bearer token after the cutover
    # made the API key the thing tests can actually mint.
    return hdr(t)


def _email():
    return f"e2e-{uuid.uuid4().hex[:8]}@example.com"


def test_trade_row_is_owner_attributed(house_book):
    client = fresh_client()
    ta = register(client, _email())["access_token"]
    the_house = house(client)

    market = create_market(client)
    yes = market["erc1155_tokens"][0][0]
    cond = market["condition_id"]["value"]
    house_book(yes, bids=(("0.6", "100"),), user=the_house)

    # A sells YES @0.6 (taker) into the house's bid (maker) → settled fill.
    client.post(
        f"/markets/{market['market_id']}/split_position",
        headers=_hdr(ta),
        json={"amount": 100_000_000},
    )
    client.post(
        "/order",
        headers=_hdr(ta),
        json={
            "token_id": yes,
            "side": "SELL",
            "price": "0.6",
            "size": 100,
            "order_type": "FAK",
        },
    )

    with fresh_test_db().read() as conn:
        row = conn.execute(
            "SELECT * FROM trades WHERE ASSET_ID = %s ORDER BY MATCH_TIME DESC LIMIT 1",
            (yes,),
        ).fetchone()
    assert row["MARKET"] == cond                  # condition_id, not token_id
    assert row["ASSET_ID"] == yes
    makers = json.loads(row["MAKER_ORDERS"])
    assert makers[0]["owner"] == the_house.user_id  # USER_ID, not eth/api_key
    assert makers[0]["maker_address"] == the_house.eth_address
    assert makers[0]["asset_id"] == yes
    assert row["TAKER_API_KEY"] and row["MAKER_API_KEY"] == the_house.api_key
    assert row["TAKER_API_KEY"] != row["MAKER_API_KEY"]
