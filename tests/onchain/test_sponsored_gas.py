import time

from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from tests.db_helpers import fresh_test_db
from tests.onchain._helpers import create_market, fresh_client, hdr, register


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


def test_the_taker_is_charged_the_fill_gas_and_the_maker_is_not():
    client = fresh_client()
    maker, taker = register(client)["api_key"], register(client)["api_key"]
    market = create_market(client)
    client.post(f"/markets/{market['market_id']}/split_position", headers=hdr(maker), json={"amount": 20_000_000}).raise_for_status()
    yes = market["erc1155_tokens"][0][0]
    client.post("/order", headers=hdr(maker), json={"token_id": yes, "side": "SELL", "price": "0.5", "size": 10}).raise_for_status()
    maker_before, taker_before = _used(maker), _used(taker)
    took = client.post("/order", headers=hdr(taker), json={"token_id": yes, "side": "BUY", "price": "0.5", "size": 10}).json()
    assert took["status"] == "matched"
    assert _used(taker) - taker_before > 50_000
    assert _used(maker) == maker_before


def test_an_exhausted_account_gets_429():
    client = fresh_client()
    key = register(client)["api_key"]
    market = create_market(client)
    with fresh_test_db().write() as conn:
        TableWrite.add_sponsored_gas(conn, key, _today(), 20_000_000)
    r = client.post("/order", headers=hdr(key), json={"token_id": market["erc1155_tokens"][0][0], "side": "BUY", "price": "0.5", "size": 10})
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) > 0


def test_a_house_taker_charges_the_maker():
    client = fresh_client()
    maker, house = register(client)["api_key"], register(client)["api_key"]
    with fresh_test_db().write() as conn:
        TableWrite.mark_user_as_bot(conn, house)
    market = create_market(client)
    client.post(f"/markets/{market['market_id']}/split_position", headers=hdr(maker), json={"amount": 20_000_000}).raise_for_status()
    yes = market["erc1155_tokens"][0][0]
    client.post("/order", headers=hdr(maker), json={"token_id": yes, "side": "SELL", "price": "0.5", "size": 10}).raise_for_status()
    maker_before, house_before = _used(maker), _used(house)
    took = client.post("/order", headers=hdr(house), json={"token_id": yes, "side": "BUY", "price": "0.5", "size": 10}).json()
    assert took["status"] == "matched"
    assert _used(maker) - maker_before > 50_000
    assert _used(house) == house_before


def test_the_reservation_is_trued_up_to_the_receipt():
    """After one fill the taker's row grew by the receipt's gas, not the 250k estimate."""
    client = fresh_client()
    maker, taker = register(client)["api_key"], register(client)["api_key"]
    market = create_market(client)
    client.post(f"/markets/{market['market_id']}/split_position", headers=hdr(maker), json={"amount": 20_000_000}).raise_for_status()
    yes = market["erc1155_tokens"][0][0]
    client.post("/order", headers=hdr(maker), json={"token_id": yes, "side": "SELL", "price": "0.5", "size": 10}).raise_for_status()
    taker_before = _used(taker)
    took = client.post("/order", headers=hdr(taker), json={"token_id": yes, "side": "BUY", "price": "0.5", "size": 10}).json()
    assert took["status"] == "matched"
    assert 50_000 < _used(taker) - taker_before < 250_000      # the receipt (measured ~168k), not the estimate
