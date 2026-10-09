"""POST /order idempotency: a repeated client_order_id replays the first order
instead of placing a second; absent client_order_id keeps legacy behavior. The
replay reconstructs fills/tradeIDs/tx-hashes from the order's trades, and a
failed original replays as success=False (spec §5.5). Auth works via X-API-Key."""

from agentpit.config import Settings
from agentpit.db.session import DbSession
from tests.onchain._helpers import create_market, fresh_client, hdr, register


def _yes(market) -> str:
    return market["erc1155_tokens"][0][0]


def _place_resting(client, tok, yes, coid) -> str:
    """Place a non-crossing BUY (rests live in an empty market, no real fill)
    and return its order id, claiming the idempotency key."""
    return client.post(
        "/order",
        headers=hdr(tok),
        json={
            "token_id": yes, "side": "BUY", "price": "0.40", "size": 10,
            "client_order_id": coid,
        },
    ).json()["orderID"]


def _inject_trade(taker_order_id, *, trade_id, size, status, match_time, tx_hash=""):
    """Insert a synthetic house fill of a BUY at 0.40 (the house bought the
    complement at 0.60) so the replay reconstruction (fills/tradeIDs/
    tx-hashes/success) can be exercised deterministically, without depending
    on a real cross + on-chain settlement."""
    db = DbSession(Settings().database_url)
    with db.write() as conn:
        conn.execute(
            "INSERT INTO trades (TRADE_ID, TAKER_ORDER_ID, TRADE_SIZE, PRICE, MATCH_KIND, "
            "STATUS, MATCH_TIME, TRANSACTION_HASH) "
            "VALUES (%s, %s, %s, 600000, 'MINT', %s, %s, %s)",
            (trade_id, taker_order_id, size, status, match_time, tx_hash),
        )


def test_same_client_order_id_places_once():
    client = fresh_client()
    tok = register(client)["access_token"]
    market = create_market(client)
    yes = _yes(market)
    body = {
        "token_id": yes, "side": "BUY", "price": "0.40", "size": 10,
        "client_order_id": "coid-abc",
    }
    r1 = client.post("/order", headers=hdr(tok), json=body).json()
    r2 = client.post("/order", headers=hdr(tok), json=body).json()
    assert r1["orderID"] == r2["orderID"]

    orders = client.get("/data/orders", headers=hdr(tok)).json()
    assert len([o for o in orders if o["asset_id"] == yes]) == 1


def test_absent_client_order_id_allows_two_orders():
    client = fresh_client()
    tok = register(client)["access_token"]
    market = create_market(client)
    yes = _yes(market)
    body = {"token_id": yes, "side": "BUY", "price": "0.40", "size": 10}
    o1 = client.post("/order", headers=hdr(tok), json=body).json()["orderID"]
    o2 = client.post("/order", headers=hdr(tok), json=body).json()["orderID"]
    assert o1 != o2


def test_x_api_key_with_client_order_id_dedups():
    """The bot's actual contract: authenticate with X-API-Key (no JWT) and dedup
    with client_order_id — exercised together end-to-end."""
    client = fresh_client()
    api_key = register(client)["user"]["api_key"]
    yes = _yes(create_market(client))
    headers = {"X-API-Key": api_key}
    body = {
        "token_id": yes, "side": "BUY", "price": "0.40", "size": 10,
        "client_order_id": "bot-coid-1",
    }
    r1 = client.post("/order", headers=headers, json=body).json()
    r2 = client.post("/order", headers=headers, json=body).json()
    assert r1["orderID"] == r2["orderID"]
    orders = client.get("/data/orders", headers=headers).json()
    assert len([o for o in orders if o["asset_id"] == yes]) == 1


def test_replay_reconstructs_filled_order():
    """A coid replay rebuilds fill amounts, tradeIDs (ORDER BY MATCH_TIME), and
    tx hashes from the order's confirmed trades."""
    client = fresh_client()
    tok = register(client)["access_token"]
    yes = _yes(create_market(client))
    oid = _place_resting(client, tok, yes, "coid-filled")
    _inject_trade(oid, trade_id="t-aaa", size=3_000_000, status="CONFIRMED",
                  match_time=1000, tx_hash="0xhash-a")
    _inject_trade(oid, trade_id="t-bbb", size=2_000_000, status="CONFIRMED",
                  match_time=2000)

    replay = client.post(
        "/order",
        headers=hdr(tok),
        json={
            "token_id": yes, "side": "BUY", "price": "0.40", "size": 10,
            "client_order_id": "coid-filled",
        },
    ).json()

    assert replay["orderID"] == oid
    assert replay["success"] is True
    assert replay["tradeIDs"] == ["t-aaa", "t-bbb"]       # ORDER BY MATCH_TIME
    assert replay["transactionsHashes"] == ["0xhash-a"]   # empty hashes dropped
    assert float(replay["takingAmount"]) == 5.0           # 5 shares filled
    assert float(replay["makingAmount"]) == 2.0           # 0.40 * 5 USDC


def test_replay_of_failed_order_reports_failure():
    """If the original order's trades are FAILED, the replay reports
    success=False with no confirmed fills (spec §5.5)."""
    client = fresh_client()
    tok = register(client)["access_token"]
    yes = _yes(create_market(client))
    oid = _place_resting(client, tok, yes, "coid-failed")
    _inject_trade(oid, trade_id="t-fail", size=4_000_000, status="FAILED",
                  match_time=1000)

    replay = client.post(
        "/order",
        headers=hdr(tok),
        json={
            "token_id": yes, "side": "BUY", "price": "0.40", "size": 10,
            "client_order_id": "coid-failed",
        },
    ).json()

    assert replay["orderID"] == oid
    assert replay["success"] is False
    assert replay["tradeIDs"] == []
    assert replay["takingAmount"] == ""
    assert replay["makingAmount"] == ""
