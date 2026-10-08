"""GET /data/trades — dual-perspective CLOB Trade[] (§8.4), secret-safe (§13)."""

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


def test_data_trades_dual_perspective_and_secret_safe():
    app = create_app()
    client = TestClient(app)
    a_email = _email()
    ra = register(client, a_email)
    b_email = _email()
    rb = register(client, b_email)
    ta, tb = ra["access_token"], rb["access_token"]
    a_uid, b_uid = ra["user"]["user_id"], rb["user"]["user_id"]

    market = client.post("/markets", json={
        "question": f"Trades {secrets.token_hex(4)}?", "description": "x",
        "outcome_labels": ["YES", "NO"], "state": "ACTIVE"}, headers=ADMIN_HDR).json()
    yes = market["erc1155_tokens"][0][0]
    cond = market["condition_id"]["value"]

    settings = Settings()
    d = Deployment.load(settings.deployment_path)
    w = Web3Client(settings, d); c = Contracts(w.web3, d); admin = OnchainAdmin(w, c)
    db = DbSession(settings.database_url)
    with db.read() as conn:
        user_b = TableRead.get_user_by_email(conn, b_email)
    admin.user_split_position(user_b.eth_key, bytes.fromhex(cond[2:]), 200_000_000)

    client.post("/order", headers=_hdr(ta), json={"token_id": yes, "side": "BUY", "price": "0.6", "size": 100})
    client.post("/order", headers=_hdr(tb), json={"token_id": yes, "side": "SELL", "price": "0.6", "size": 100})

    # Taker B's view.
    rb_trades = client.get("/data/trades", headers=_hdr(tb))
    bbody = rb_trades.json()
    assert bbody["next_cursor"] == "LTE="
    assert bbody["count"] == 1
    t = bbody["data"][0]
    assert t["trader_side"] == "TAKER"
    assert t["market"] == cond and t["asset_id"] == yes
    assert t["size"] == "100" and t["price"] == "0.6" and t["status"] == "MATCHED"
    assert t["owner"] == b_uid
    # The counterparty (maker A) owner is A's USER_ID — NOT an api_key.
    assert t["maker_orders"][0]["owner"] == a_uid
    assert t["maker_orders"][0]["matched_amount"] == "100"

    # Maker A's view of the SAME match.
    abody = client.get("/data/trades", headers=_hdr(ta)).json()
    assert abody["count"] == 1
    assert abody["data"][0]["trader_side"] == "MAKER"
    assert abody["data"][0]["owner"] == a_uid

    # Secret-safety (§13): neither the api_key column names NOR the actual
    # api_key VALUES (incl. the counterparty's, surfaced cross-perspective)
    # ever appear in a response body.
    with db.read() as conn:
        a_api_key = TableRead.get_user_by_email(conn, a_email).api_key
        b_api_key = TableRead.get_user_by_email(conn, b_email).api_key
    for raw in (rb_trades.text, client.get("/data/trades", headers=_hdr(ta)).text):
        assert "TAKER_API_KEY" not in raw and "MAKER_API_KEY" not in raw
        assert "api_key" not in raw.lower()
        assert a_api_key not in raw and b_api_key not in raw


def test_data_trades_empty_and_requires_auth():
    app = create_app()
    with TestClient(app) as client:
        body = register(client, _email())
        assert client.get("/data/trades", headers=_hdr(body["access_token"])).json() == {
            "limit": 100, "count": 0, "next_cursor": "LTE=", "data": []
        }
        assert client.get("/data/trades").status_code == 401
