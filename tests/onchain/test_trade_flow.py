"""Live anvil integration: register → market → fill against the house → settle on-chain.

Exercises the full happy path against a running anvil + deployed stack.
"""

import time
import uuid
from decimal import Decimal

from fastapi.testclient import TestClient

from agentpit.api.deps import get_db_session, get_onchain_admin
from agentpit.db.table_read import TableRead
from tests.onchain._helpers import (
    create_market,
    fresh_client,
    fund_direct_sends,
    hdr,
    house,
    order_service,
    register,
    send_as,
)


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


def _micro(amount: str) -> int:
    return int(Decimal(amount) * 1_000_000)


def _settled(svc, db, order_id: str) -> list[str]:
    statuses = ["PENDING"]
    for _ in range(50):
        svc.settle_pending(int(time.time()) + 61)
        with db.read() as conn:
            statuses = [
                r["STATUS"]
                for r in conn.execute(
                    "SELECT STATUS FROM trades WHERE TAKER_ORDER_ID = %s ORDER BY MATCH_TIME",
                    (order_id,),
                ).fetchall()
            ]
        if "PENDING" not in statuses:
            break
        time.sleep(0.2)
    return statuses


def _order_row(db, order_id: str) -> tuple[str, int]:
    with db.read() as conn:
        r = conn.execute(
            "SELECT STATUS, REMAINING_AMOUNT FROM orders WHERE ORDER_ID = %s",
            (order_id,),
        ).fetchone()
    return r["STATUS"], r["REMAINING_AMOUNT"]


def test_all_four_mappings_settle_against_the_house_to_the_micro(house_book):
    client = fresh_client()
    agent = register(client)
    the_house = house(client)
    admin = client.app.dependency_overrides[get_onchain_admin]()
    market = create_market(client)
    yes, no = (t for t, _ in market["erc1155_tokens"])
    complement = {yes: no, no: yes}
    house_book(
        yes,
        bids=(("0.40", "30"), ("0.39", "50.0000007")),
        asks=(("0.60", "10"), ("0.61", "10"), ("0.62", "13.3333337")),
        user=the_house,
    )

    def balances(address: str) -> dict[str, int]:
        return {
            "usd": admin.usd_balance(address),
            yes: admin.ctf_balance(address, int(yes)),
            no: admin.ctf_balance(address, int(no)),
        }

    fills = []
    for token, side, price, size in (
        (yes, "BUY", "0.62", 25),
        (yes, "SELL", "0.39", 10),
        (no, "BUY", "0.61", 20),
        (no, "SELL", "0.37", 10),
    ):
        agent0, house0 = balances(agent["user"]["eth_address"]), balances(
            the_house.eth_address
        )
        r = client.post(
            "/order",
            headers=hdr(agent["api_key"]),
            json={
                "token_id": token,
                "side": side,
                "price": price,
                "size": size,
                "order_type": "FAK",
            },
        ).json()
        assert r["success"] and r["status"] == "matched", r
        making, taking = _micro(r["makingAmount"]), _micro(r["takingAmount"])
        agent1, house1 = balances(agent["user"]["eth_address"]), balances(
            the_house.eth_address
        )
        if side == "BUY":
            moved = (
                {"usd": -making, token: taking},
                {"usd": making - taking, complement[token]: taking},
            )
        else:
            moved = ({"usd": taking, token: -making}, {"usd": -taking, token: making})
        assert (
            {k: agent1[k] - agent0[k] for k in agent1},
            {k: house1[k] - house0[k] for k in house1},
        ) == tuple({k: m.get(k, 0) for k in agent1} for m in moved), (token, side)
        fills.append((r["makingAmount"], r["takingAmount"]))

    assert fills == [
        ("15.2", "25"),
        ("10", "4"),
        ("12", "20"),
        ("8.333334", "3.166667"),
    ]


def test_a_resting_order_fills_to_exhaustion_over_several_sweeps(house_book):
    client = fresh_client()
    agent = register(client)
    admin = client.app.dependency_overrides[get_onchain_admin]()
    db = client.app.dependency_overrides[get_db_session]()
    market = create_market(client)
    yes = market["erc1155_tokens"][0][0]
    book = house_book(yes, asks=(("0.5", "10"),), user=house(client))
    svc = order_service(client)
    usd0 = admin.usd_balance(agent["user"]["eth_address"])

    placed = client.post(
        "/order",
        headers=hdr(agent["api_key"]),
        json={"token_id": yes, "side": "BUY", "price": "0.5", "size": 30},
    ).json()
    assert (placed["status"], placed["takingAmount"]) == ("live", "10"), placed
    for size, left in (("12", 8_000_000), ("15", 0)):
        book.apply_price_change_entry(
            {"asset_id": book.asset_id, "side": "SELL", "price": "0.5", "size": size}
        )
        svc.sweep()
        assert _order_row(db, placed["orderID"])[1] == left

    assert _settled(svc, db, placed["orderID"]) == ["CONFIRMED"] * 3
    assert _order_row(db, placed["orderID"]) == ("matched", 0)
    assert admin.ctf_balance(agent["user"]["eth_address"], int(yes)) == 30_000_000
    assert admin.usd_balance(agent["user"]["eth_address"]) == usd0 - 15_000_000


def test_an_underfunded_resting_buy_fails_and_its_remainder_is_cancelled(house_book):
    client = fresh_client()
    agent = register(client)
    admin = client.app.dependency_overrides[get_onchain_admin]()
    db = client.app.dependency_overrides[get_db_session]()
    market = create_market(client)
    yes = market["erc1155_tokens"][0][0]
    book = house_book(yes, asks=(("0.6", "40"),), user=house(client))
    svc = order_service(client)

    placed = client.post(
        "/order",
        headers=hdr(agent["api_key"]),
        json={"token_id": yes, "side": "BUY", "price": "0.5", "size": 100},
    ).json()
    assert placed["status"] == "live", placed
    with db.read() as conn:
        key = TableRead.get_user_by_api_key(conn, agent["api_key"]).eth_key
    fund_direct_sends(client, agent["user"]["eth_address"])
    cash = admin.usd_balance(agent["user"]["eth_address"])
    admin.user_split_position(
        key, bytes.fromhex(market["condition_id"]["value"][2:]), cash - 10_000_000
    )
    book.apply_price_change_entry(
        {"asset_id": book.asset_id, "side": "SELL", "price": "0.5", "size": "40"}
    )
    svc.sweep()

    assert _settled(svc, db, placed["orderID"]) == ["FAILED"]
    assert _order_row(db, placed["orderID"]) == ("cancelled", 60_000_000)


def test_a_reverted_fill_fails_the_order(house_book, monkeypatch):
    """A matchOrders mined with status 0 moved nothing, so the placement fails:
    its trade is FAILED and the answer is not a success. The agent revokes the
    exchange's apUSD allowance first, and the admin send skips the gas estimate
    (a static limit), so the revert lands in a mined receipt instead of failing
    at estimation, as when the chain changes between estimate and inclusion."""
    client = fresh_client()
    agent = register(client)
    address = agent["user"]["eth_address"]
    admin = client.app.dependency_overrides[get_onchain_admin]()
    db = client.app.dependency_overrides[get_db_session]()
    market = create_market(client)
    yes = market["erc1155_tokens"][0][0]
    house_book(yes, asks=(("0.6", "100"),), user=house(client))
    with db.read() as conn:
        user = TableRead.get_user_by_api_key(conn, agent["api_key"])
    usd = admin._contracts.usd  # noqa: SLF001
    send_as(admin, user, usd.functions.approve(admin._contracts.exchange.address, 0))  # noqa: SLF001
    sender = admin._client.admin_sender  # noqa: SLF001
    submit = sender.submit
    monkeypatch.setattr(sender, "submit", lambda fn, **kw: submit(fn, gas=2_000_000, **kw))
    usd0 = admin.usd_balance(address)

    r = client.post(
        "/order",
        headers=hdr(agent["api_key"]),
        json={"token_id": yes, "side": "BUY", "price": "0.6", "size": 100, "order_type": "FAK"},
    ).json()

    assert r["success"] is False and "reverted" in r["errorMsg"], r
    assert r["transactionsHashes"] == []
    assert _settled(order_service(client), db, r["orderID"]) == ["FAILED"]
    assert admin.usd_balance(address) == usd0
    assert admin.ctf_balance(address, int(yes)) == 0
