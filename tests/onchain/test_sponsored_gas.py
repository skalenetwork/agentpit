import time

from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from tests.db_helpers import fresh_test_db
from tests.onchain._helpers import create_market, fresh_client, hdr, house, register


def _today() -> int:
    return int(time.time()) // 86_400


def _used(api_key: str) -> int:
    """The account's sponsored gas today.

    Read on both sides of the fill rather than compared with zero: onboarding
    and split/merge are sponsored too, so every `register()`ed account starts
    with its approvals' gas on the row, and a maker with its split's as well.
    """
    with fresh_test_db().read() as conn:
        return TableRead.sponsored_gas_used(conn, api_key, _today())


def test_a_fill_charges_the_agent_a_flat_250k_and_the_house_nothing(house_book):
    """Every fill is an admin-paid matchOrders with the agent as taker: its day is
    charged a flat 250k inside the fill, never trued up to the receipt."""
    client = fresh_client()
    agent, the_house = register(client)["api_key"], house(client)
    market = create_market(client)
    yes = market["erc1155_tokens"][0][0]
    house_book(yes, asks=(("0.5", "10"),), user=the_house)
    agent_before, house_before = _used(agent), _used(the_house.api_key)
    took = client.post(
        "/order",
        headers=hdr(agent),
        json={"token_id": yes, "side": "BUY", "price": "0.5", "size": 10, "order_type": "FAK"},
    ).json()
    assert took["status"] == "matched", took
    assert _used(agent) - agent_before == 250_000
    assert _used(the_house.api_key) == house_before


def test_an_exhausted_account_gets_429():
    client = fresh_client()
    key = register(client)["api_key"]
    market = create_market(client)
    with fresh_test_db().write() as conn:
        TableWrite.add_sponsored_gas(conn, key, _today(), 20_000_000)
    r = client.post("/order", headers=hdr(key), json={"token_id": market["erc1155_tokens"][0][0], "side": "BUY", "price": "0.5", "size": 10})
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) > 0
