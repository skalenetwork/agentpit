"""Replaces deleted tests/api/test_resolution.py.

Resolve a market on-chain, then verify the redeem path pays out only the
holders of the winning outcome.
"""

from agentpit.config import Settings
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.onchain.admin import OnchainAdmin
from agentpit.onchain.contracts import Contracts
from agentpit.onchain.deployment import Deployment
from agentpit.onchain.web3_client import Web3Client

from tests.onchain._helpers import ADMIN_HDR, create_market, fresh_client, hdr, register


def _admin() -> OnchainAdmin:
    s = Settings()
    d = Deployment.load(s.deployment_path)
    w = Web3Client(s, d)
    c = Contracts(w.web3, d)
    return OnchainAdmin(w, c)


def test_resolve_then_winner_redeems():
    """A and B both split; YES wins; A redeems and recovers full collateral."""
    client = fresh_client()
    user_a = register(client)
    user_b = register(client)
    market = create_market(client)
    mid = market["market_id"]
    yes_id = int(market["erc1155_tokens"][0][0])
    no_id = int(market["erc1155_tokens"][1][0])

    split_amount = 100_000_000
    client.post(
        f"/markets/{mid}/split_position",
        headers=hdr(user_a["access_token"]),
        json={"amount": split_amount},
    ).raise_for_status()
    client.post(
        f"/markets/{mid}/split_position",
        headers=hdr(user_b["access_token"]),
        json={"amount": 50_000_000},
    ).raise_for_status()

    # Resolve YES wins. The on-chain CTF only pays out once the oracle (admin)
    # calls reportPayouts; do that via the admin helper.
    admin = _admin()
    settings = Settings()
    db = DbSession(settings.database_url)
    with db.read() as conn:
        user_a_row = TableRead.get_user_by_email(conn, user_a["user"]["email"])
    question_id = bytes.fromhex(market["question_id"][2:])
    # admin.reportPayouts(questionId, [1, 0]) — YES wins
    fn = admin._contracts.ctf.functions.reportPayouts(question_id, [1, 0])
    from agentpit.onchain.user_wallet import send_admin_tx

    send_admin_tx(admin._client, fn)

    # Mark resolved in DB so position service allows redeem.
    resolve = client.post(
        f"/markets/{mid}/resolve",
        json={"winning_outcome_index": 0},
        headers=ADMIN_HDR,
    ).json()
    assert resolve["market_state"] == "RESOLVED"

    # Pre-redeem balances
    pre_a_usd = admin.usd_balance(user_a["user"]["eth_address"])
    pre_a_yes = admin.ctf_balance(user_a["user"]["eth_address"], yes_id)
    assert pre_a_yes == split_amount

    redeem = client.post(
        f"/markets/{mid}/redeem_position", headers=hdr(user_a["access_token"])
    )
    assert redeem.status_code == 200, redeem.text

    # Winner gets the full collateral back (split_amount apUSD), YES + NO burned
    post_a_usd = admin.usd_balance(user_a["user"]["eth_address"])
    post_a_yes = admin.ctf_balance(user_a["user"]["eth_address"], yes_id)
    post_a_no = admin.ctf_balance(user_a["user"]["eth_address"], no_id)
    assert post_a_usd == pre_a_usd + split_amount
    assert post_a_yes == 0
    assert post_a_no == 0


def test_redeem_before_resolve_is_400():
    client = fresh_client()
    user = register(client)
    market = create_market(client)
    resp = client.post(
        f"/markets/{market['market_id']}/redeem_position",
        headers=hdr(user["access_token"]),
    )
    assert resp.status_code == 400
    assert "not resolved" in resp.json()["detail"]
