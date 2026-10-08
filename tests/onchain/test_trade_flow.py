"""Live anvil integration: register → market → split → match → settle on-chain.

Exercises the full happy path against a running anvil + deployed stack.
"""

import secrets
import uuid

from fastapi.testclient import TestClient

from tests.onchain._helpers import ADMIN_HDR, hdr, register


def _hdr(token: str) -> dict[str, str]:
    # See the note in _helpers.hdr: the suite authenticates with X-API-Key.
    return hdr(token)


def _email() -> str:
    return f"e2e-{uuid.uuid4().hex[:8]}@example.com"


def test_register_funds_user_and_grants_approvals():
    # Imports inside the test so the conftest env tweaks land first.
    from agentpit.api.app import create_app
    from agentpit.config import Settings
    from agentpit.onchain.contracts import Contracts
    from agentpit.onchain.deployment import Deployment
    from agentpit.onchain.web3_client import Web3Client

    app = create_app()
    client = TestClient(app)

    body = register(client, _email())
    eth = body["user"]["eth_address"]
    assert body["user"]["onboarded_at"] is not None

    settings = Settings()
    deployment = Deployment.load(settings.deployment_path)
    w3 = Web3Client(settings, deployment)
    contracts = Contracts(w3.web3, deployment)
    assert contracts.usd.functions.balanceOf(eth).call() == deployment.signup_grant_raw
    assert contracts.usd.functions.allowance(eth, deployment.exchange).call() > 0
    assert contracts.usd.functions.allowance(eth, deployment.ctf).call() > 0
    assert contracts.ctf.functions.isApprovedForAll(eth, deployment.exchange).call()


def test_match_settles_on_chain():
    from agentpit.api.app import create_app
    from agentpit.config import Settings
    from agentpit.db.session import DbSession
    from agentpit.db.table_read import TableRead
    from agentpit.onchain.admin import OnchainAdmin
    from agentpit.onchain.contracts import Contracts
    from agentpit.onchain.deployment import Deployment
    from agentpit.onchain.web3_client import Web3Client

    app = create_app()
    client = TestClient(app)

    a_email = _email()
    b_email = _email()
    ra = register(client, a_email)
    rb = register(client, b_email)
    ta, tb = ra["access_token"], rb["access_token"]
    ea, eb = ra["user"]["eth_address"], rb["user"]["eth_address"]

    market = client.post(
        "/markets",
        json={
            "question": f"Live test {secrets.token_hex(4)}?",
            "description": "YES if test passes",
            "outcome_labels": ["YES", "NO"],
            "state": "ACTIVE",
        },
        headers=ADMIN_HDR,
    ).json()
    yes_id = int(market["erc1155_tokens"][0][0])

    # Give B some YES tokens via splitPosition so B can SELL.
    settings = Settings()
    d = Deployment.load(settings.deployment_path)
    w = Web3Client(settings, d)
    c = Contracts(w.web3, d)
    admin = OnchainAdmin(w, c)
    db = DbSession(settings.database_url)
    with db.read() as conn:
        user_b = TableRead.get_user_by_email(conn, b_email)
    cond = bytes.fromhex(market["condition_id"]["value"][2:])
    admin.user_split_position(user_b.eth_key, cond, 200_000_000)

    a_pre_usd = admin.usd_balance(ea)
    b_pre_yes = admin.ctf_balance(eb, yes_id)

    pa = client.post(
        "/order",
        headers=_hdr(ta),
        json={
            "token_id": market["erc1155_tokens"][0][0],
            "side": "BUY",
            "price": "0.6",
            "size": 100,
        },
    ).json()
    assert pa["success"] and pa["status"] == "live"

    pb = client.post(
        "/order",
        headers=_hdr(tb),
        json={
            "token_id": market["erc1155_tokens"][0][0],
            "side": "SELL",
            "price": "0.6",
            "size": 100,
        },
    ).json()
    assert pb["success"], pb
    assert pb["status"] == "matched"
    assert pb["makingAmount"] == "100"          # SELL taker gave 100 shares
    assert pb["takingAmount"] == "60"           # received 60 apUSD (100 @ 0.6)
    assert pb["transactionsHashes"]             # non-empty list
    assert "filledSize" not in pb

    # Verify on-chain settlement: A paid 60M apUSD, received 100M YES tokens.
    assert admin.usd_balance(ea) == a_pre_usd - 60_000_000
    assert admin.ctf_balance(ea, yes_id) == 100_000_000
    assert admin.ctf_balance(eb, yes_id) == b_pre_yes - 100_000_000

    # /last-trade-price reflects the settled match: maker price 0.6, taker side SELL.
    yes_token = market["erc1155_tokens"][0][0]
    ltp = client.get(f"/last-trade-price?token_id={yes_token}").json()
    assert ltp == {"price": "0.6", "side": "SELL"}


def test_complementary_buys_mint_via_split():
    """Two BUYs on opposite outcomes whose prices sum to >= 1.00 should match.

    The off-chain matcher must detect the complement and the on-chain
    CTFExchange must MINT a fresh pair via splitPosition, delivering YES
    to the YES-buyer and NO to the NO-buyer.
    """
    from agentpit.api.app import create_app
    from agentpit.config import Settings
    from agentpit.onchain.admin import OnchainAdmin
    from agentpit.onchain.contracts import Contracts
    from agentpit.onchain.deployment import Deployment
    from agentpit.onchain.web3_client import Web3Client

    app = create_app()
    client = TestClient(app)

    a_email = _email()
    b_email = _email()
    ra = register(client, a_email)
    rb = register(client, b_email)
    ta, tb = ra["access_token"], rb["access_token"]
    ea, eb = ra["user"]["eth_address"], rb["user"]["eth_address"]

    market = client.post(
        "/markets",
        json={
            "question": f"Complement test {secrets.token_hex(4)}?",
            "description": "MINT match path",
            "outcome_labels": ["YES", "NO"],
            "state": "ACTIVE",
        },
        headers=ADMIN_HDR,
    ).json()
    yes_id = int(market["erc1155_tokens"][0][0])
    no_id = int(market["erc1155_tokens"][1][0])

    settings = Settings()
    d = Deployment.load(settings.deployment_path)
    w = Web3Client(settings, d)
    c = Contracts(w.web3, d)
    admin = OnchainAdmin(w, c)

    a_pre_usd = admin.usd_balance(ea)
    b_pre_usd = admin.usd_balance(eb)

    # A: resting BUY YES @ 0.30, size 100 shares.
    pa = client.post(
        "/order",
        headers=_hdr(ta),
        json={
            "token_id": market["erc1155_tokens"][0][0],
            "side": "BUY",
            "price": "0.3",
            "size": 100,
        },
    ).json()
    assert pa["success"] and pa["status"] == "live", pa

    # B: incoming BUY NO @ 0.70, size 50 shares. Sum is 1.00 → should mint 50M.
    pb = client.post(
        "/order",
        headers=_hdr(tb),
        json={
            "token_id": market["erc1155_tokens"][1][0],
            "side": "BUY",
            "price": "0.7",
            "size": 50,
        },
    ).json()
    assert pb["success"], pb
    assert pb["status"] == "matched", pb
    assert pb["makingAmount"] == "35"           # NO buyer paid 50 @ 0.70
    assert pb["transactionsHashes"], pb

    # A paid 50M * 0.30 = 15M apUSD; B paid 50M * 0.70 = 35M apUSD.
    assert admin.usd_balance(ea) == a_pre_usd - 15_000_000
    assert admin.usd_balance(eb) == b_pre_usd - 35_000_000
    # MINT delivered 50M of each token to the respective buyer.
    assert admin.ctf_balance(ea, yes_id) == 50_000_000
    assert admin.ctf_balance(eb, no_id) == 50_000_000
    # A's order remains live with 50 shares (50M base units) outstanding.
    yes_token = market["erc1155_tokens"][0][0]
    book = client.get(f"/book?token_id={yes_token}").json()
    assert any(lvl["size"] == "50" for lvl in book["bids"]), book
