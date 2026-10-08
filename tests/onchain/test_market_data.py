"""GET /midpoint, /price, /last-trade-price (§8.7). Live-chain: resting
orders give a deterministic book; 404 paths when book/trades are absent."""

from tests.onchain._helpers import create_market, fresh_client, fund_direct_sends, register, hdr


def _yes(market) -> str:
    return market["erc1155_tokens"][0][0]


def _give_yes_tokens(client, market, user_eth_key, amount: int = 10_000_000):
    """Give a user YES tokens via splitPosition so they can place SELL orders."""
    from agentpit.config import Settings
    from agentpit.onchain.admin import OnchainAdmin
    from agentpit.onchain.contracts import Contracts
    from agentpit.onchain.deployment import Deployment
    from agentpit.onchain.web3_client import Web3Client

    settings = Settings()
    d = Deployment.load(settings.deployment_path)
    w = Web3Client(settings, d)
    c = Contracts(w.web3, d)
    admin = OnchainAdmin(w, c)
    cond = bytes.fromhex(market["condition_id"]["value"][2:])
    fund_direct_sends(client, user_eth_key.address)
    admin.user_split_position(user_eth_key, cond, amount)


def test_midpoint_and_price_from_book():
    client = fresh_client()
    ra = register(client)
    rb = register(client)
    tok_buy = ra["access_token"]
    tok_sell = rb["access_token"]
    user_b_key = rb.get("user", {}).get("eth_key") or rb.get("eth_key")

    market = create_market(client)
    yes = _yes(market)

    # Give user B some YES tokens so they can SELL.
    from agentpit.config import Settings
    from agentpit.db.session import DbSession
    from agentpit.db.table_read import TableRead
    settings = Settings()
    db = DbSession(settings.database_url)
    with db.read() as conn:
        user_b = TableRead.get_user_by_email(conn, rb["user"]["email"])
    _give_yes_tokens(client, market, user_b.eth_key)

    # Resting bid 0.40 (user A BUY) and ask 0.60 (user B SELL); they don't cross.
    r1 = client.post("/order", headers=hdr(tok_buy), json={"token_id": yes, "side": "BUY", "price": "0.40", "size": 5})
    r2 = client.post("/order", headers=hdr(tok_sell), json={"token_id": yes, "side": "SELL", "price": "0.60", "size": 5})
    assert r1.json()["status"] == "live", r1.json()
    assert r2.json()["status"] == "live", r2.json()

    assert client.get(f"/midpoint?token_id={yes}").json() == {"mid": "0.5"}
    assert client.get(f"/price?token_id={yes}&side=BUY").json() == {"price": "0.6"}
    assert client.get(f"/price?token_id={yes}&side=SELL").json() == {"price": "0.4"}


def test_midpoint_404_without_book():
    client = fresh_client()
    tok = register(client)["access_token"]
    market = create_market(client)
    yes = _yes(market)
    client.post("/order", headers=hdr(tok), json={"token_id": yes, "side": "BUY", "price": "0.40", "size": 5})
    # Only a bid → no midpoint; no trades → last-trade-price 404.
    assert client.get(f"/midpoint?token_id={yes}").status_code == 404
    assert client.get(f"/last-trade-price?token_id={yes}").status_code == 404


def test_resting_orders_feed_the_book():
    client = fresh_client()
    ra = register(client)
    rb = register(client)
    tok_buy = ra["access_token"]
    tok_sell = rb["access_token"]

    market = create_market(client)
    yes = _yes(market)

    # Give user B some YES tokens so they can SELL.
    from agentpit.config import Settings
    from agentpit.db.session import DbSession
    from agentpit.db.table_read import TableRead
    settings = Settings()
    db = DbSession(settings.database_url)
    with db.read() as conn:
        user_b = TableRead.get_user_by_email(conn, rb["user"]["email"])
    _give_yes_tokens(client, market, user_b.eth_key)

    r1 = client.post("/order", headers=hdr(tok_buy), json={"token_id": yes, "side": "BUY", "price": "0.40", "size": 5})
    r2 = client.post("/order", headers=hdr(tok_sell), json={"token_id": yes, "side": "SELL", "price": "0.60", "size": 5})
    assert r1.json()["status"] == "live", r1.json()
    assert r2.json()["status"] == "live", r2.json()

    body = client.get(f"/book?token_id={yes}").json()
    assert len(body["bids"]) == 1 and len(body["asks"]) == 1
