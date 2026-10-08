"""GET /activity (§8.10) + SPLIT/REDEEM logging (missing-feature #01).
Public-by-address. Live-chain."""

import secrets
import uuid

from fastapi.testclient import TestClient

from agentpit.api.app import create_app
from agentpit.config import Settings
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.onchain.admin import OnchainAdmin
from agentpit.onchain.contracts import Contracts
from agentpit.onchain.deployment import Deployment
from agentpit.onchain.web3_client import Web3Client

from tests.onchain._helpers import ADMIN_HDR, fund_direct_sends, hdr, register


def _hdr(t):
    # One definition of the suite's credential lives in _helpers; six local
    # copies is how this file kept sending a bearer token after the cutover
    # made the API key the thing tests can actually mint.
    return hdr(t)


def _email():
    return f"e2e-{uuid.uuid4().hex[:8]}@example.com"


def test_activity_has_split_and_trade_rows():
    app = create_app()
    client = TestClient(app)
    ra = register(client, _email())
    b_email = _email()
    rb = register(client, b_email)
    ta, tb = ra["access_token"], rb["access_token"]
    a_addr = ra["user"]["eth_address"]

    market = client.post("/markets", json={
        "question": f"Act {secrets.token_hex(4)}?", "description": "x",
        "outcome_labels": ["YES", "NO"], "state": "ACTIVE"}, headers=ADMIN_HDR).json()
    mid = market["market_id"]
    yes = market["erc1155_tokens"][0][0]
    cond = market["condition_id"]["value"]

    # A splits collateral → SPLIT activity row.
    client.post(f"/markets/{mid}/split_position", headers=_hdr(ta), json={"amount": 50_000_000})

    # B funded → SELLs into A's BUY → settled TRADE for both.
    settings = Settings()
    d = Deployment.load(settings.deployment_path)
    w = Web3Client(settings, d); c = Contracts(w.web3, d); admin = OnchainAdmin(w, c)
    db = DbSession(settings.database_url)
    with db.read() as conn:
        user_b = TableRead.get_user_by_email(conn, b_email)
    fund_direct_sends(client, user_b.eth_address)
    admin.user_split_position(user_b.eth_key, bytes.fromhex(cond[2:]), 200_000_000)
    client.post("/order", headers=_hdr(ta), json={"token_id": yes, "side": "BUY", "price": "0.6", "size": 100})
    client.post("/order", headers=_hdr(tb), json={"token_id": yes, "side": "SELL", "price": "0.6", "size": 100})

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
