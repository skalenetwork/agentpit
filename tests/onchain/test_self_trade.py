from agentpit.api.deps import get_db_session
from agentpit.db.table_write import TableWrite
from agentpit.services.agent_accounts import AgentAccounts
from tests.onchain._helpers import _auth_service, create_market, fresh_client, hdr, register


def test_an_agent_cannot_fill_its_owners_order_but_a_stranger_can():
    client = fresh_client()
    db = client.app.dependency_overrides[get_db_session]()  # type: ignore[attr-defined]
    owner = register(client)
    with db.write() as conn:
        TableWrite.set_workos_user_id(conn, owner["user"]["user_id"], "user_selftrade")
    agent = AgentAccounts(db, _auth_service(client)._onboard_new_account).create_api_agent("user_selftrade")
    stranger = register(client)["api_key"]
    market = create_market(client)
    client.post(f"/markets/{market['market_id']}/split_position", headers=hdr(owner["api_key"]),
                json={"amount": 20_000_000}).raise_for_status()
    yes = market["erc1155_tokens"][0][0]
    ask = client.post("/order", headers=hdr(owner["api_key"]),
                      json={"token_id": yes, "side": "SELL", "price": "0.5", "size": 10}).json()
    assert ask["status"] == "live"

    own = client.post("/order", headers=hdr(agent.api_key),
                      json={"token_id": yes, "side": "BUY", "price": "0.5", "size": 10, "order_type": "FAK"})
    assert own.status_code == 400            # FAK with nothing it may match
    other = client.post("/order", headers=hdr(stranger),
                        json={"token_id": yes, "side": "BUY", "price": "0.5", "size": 10}).json()
    assert other["success"] and other["status"] == "matched" and other["transactionsHashes"]
