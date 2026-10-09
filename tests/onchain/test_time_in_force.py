from fastapi.testclient import TestClient

from tests.db_helpers import fresh_test_db
from tests.onchain._helpers import create_market, fresh_client, hdr, house, register

FOK_SHORT = "order couldn't be fully filled. FOK orders are fully filled or killed."
FAK_EMPTY = "no orders found to match with FAK order. FAK orders are partially filled or killed if no match is found."


def _book(house_book) -> tuple[TestClient, str, str]:
    client = fresh_client()
    taker = register(client)["api_key"]
    market = create_market(client)
    yes = market["erc1155_tokens"][0][0]
    house_book(yes, asks=(("0.4", "50"),), user=house(client))
    return client, taker, yes


def _order(client: TestClient, key: str, token: str, price: str, size: int, order_type: str) -> tuple[int, dict]:
    response = client.post(
        "/order",
        headers=hdr(key),
        json={"token_id": token, "side": "BUY", "price": price, "size": size, "order_type": order_type},
    )
    return response.status_code, response.json()


def _order_rows(key: str) -> int:
    with fresh_test_db().read() as conn:
        return len(conn.execute("SELECT ORDER_ID FROM orders WHERE API_KEY = %s", (key,)).fetchall())


def test_fak_fills_what_it_can_and_kills_the_rest(house_book):
    client, taker, yes = _book(house_book)

    code, empty = _order(client, taker, yes, "0.3", 10, "FAK")
    assert (code, empty["detail"]) == (400, FAK_EMPTY)
    assert _order_rows(taker) == 0

    code, placed = _order(client, taker, yes, "0.5", 100, "FAK")
    assert code == 200 and placed["success"]
    assert placed["status"] == "matched"
    assert placed["takingAmount"] == "50"
    assert client.get("/data/orders", headers=hdr(taker)).json() == []


def test_fok_fills_in_full_or_not_at_all(house_book):
    client, taker, yes = _book(house_book)

    code, short = _order(client, taker, yes, "0.5", 100, "FOK")
    assert (code, short["detail"]) == (400, FOK_SHORT)
    assert _order_rows(taker) == 0

    code, placed = _order(client, taker, yes, "0.5", 50, "FOK")
    assert code == 200 and placed["status"] == "matched"
    assert placed["takingAmount"] == "50"
