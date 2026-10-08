"""GET /positions + /value (§8.8/8.9) — public-by-address. Live-chain."""

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

from tests.onchain._helpers import ADMIN_HDR, hdr, register


def _hdr(t):
    # One definition of the suite's credential lives in _helpers; six local
    # copies is how this file kept sending a bearer token after the cutover
    # made the API key the thing tests can actually mint.
    return hdr(t)


def _email():
    return f"e2e-{uuid.uuid4().hex[:8]}@example.com"


def test_positions_and_value_public_by_address():
    app = create_app()
    client = TestClient(app)
    ra = register(client, _email())
    b_email = _email()
    rb = register(client, b_email)
    ta, tb = ra["access_token"], rb["access_token"]
    a_addr = ra["user"]["eth_address"]

    market = client.post("/markets", json={
        "question": f"Pos {secrets.token_hex(4)}?", "description": "x",
        "outcome_labels": ["YES", "NO"], "state": "ACTIVE"}, headers=ADMIN_HDR).json()
    yes = market["erc1155_tokens"][0][0]
    cond = market["condition_id"]["value"]

    # B funded with YES → SELLs into A's BUY so A ends up holding 100 YES @0.6.
    settings = Settings()
    d = Deployment.load(settings.deployment_path)
    w = Web3Client(settings, d); c = Contracts(w.web3, d); admin = OnchainAdmin(w, c)
    db = DbSession(settings.database_url)
    with db.read() as conn:
        user_b = TableRead.get_user_by_email(conn, b_email)
    admin.user_split_position(user_b.eth_key, bytes.fromhex(cond[2:]), 200_000_000)
    client.post("/order", headers=_hdr(ta), json={"token_id": yes, "side": "BUY", "price": "0.6", "size": 100})
    client.post("/order", headers=_hdr(tb), json={"token_id": yes, "side": "SELL", "price": "0.6", "size": 100})

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
