"""A split or claim that mined after we stopped waiting for it, on anvil.

`send_as_user` is wrapped to send for real, then raise as if the answer had
been lost (a receipt timeout, or a read timeout on the broadcast). Checks that:

- the caller gets 503 "not confirmed yet ... do not repeat it", not a 500;
- the intent row stays and no history row is written yet;
- a second request on the market is a 409, not a second split or a "nothing
  to claim" for a claim that was paid;
- `reconcile_pending_user_txs` turns the intent into the history row, at the
  claim's exact payout.

The unit cases (a refusal, a revert, a TTL, a row that fails) are in
tests/services/test_position_service.py and test_pending_user_txs.py.
"""

from __future__ import annotations

import pytest
import requests
from fastapi.testclient import TestClient
from web3.exceptions import TimeExhausted

from agentpit.api.app import create_app
from agentpit.api.deps import get_db_session, get_onchain_admin
from agentpit.config import Settings
from agentpit.db.table_read import TableRead
from agentpit.services.account_service import AccountService
from agentpit.services.pending_user_txs import reconcile_pending_user_txs
from tests.onchain._helpers import (
    drain_native_balance,
    dry_winner,
    hdr,
    onboarded_account,
    pending_user_txs,
    redeem_amounts,
    synced_market,
    tx_rows,
)


def _app():
    """The real app, its admin and its database. The lifespan is not run, so
    no background pass reconciles anything behind the test's back."""
    client = TestClient(create_app(Settings()), raise_server_exceptions=False)
    overrides = client.app.dependency_overrides  # type: ignore[attr-defined]
    return client, overrides[get_onchain_admin](), overrides[get_db_session]()


def _lose_the_answer(monkeypatch, admin, error: Exception) -> list:
    """Send every user transaction for real, then raise `error` in place of
    its receipt. Returns the receipts the caller never saw."""
    real = admin.send_as_user
    unseen: list = []

    def send_as_user(user_account, fn, **kwargs):
        unseen.append(real(user_account, fn, **kwargs))
        raise error

    monkeypatch.setattr(admin, "send_as_user", send_as_user)
    return unseen


def _hash(receipt) -> str:
    return "0x" + bytes(receipt["transactionHash"]).hex()


def test_a_claim_whose_receipt_was_lost_is_settled_at_its_exact_payout(monkeypatch):
    client, admin, db = _app()
    market, user = dry_winner(db, admin)
    mid = market.market_id
    tokens = [int(t) for t, _label in market.erc1155_tokens]
    usd_before = admin.usd_balance(user.eth_address)
    unseen = _lose_the_answer(monkeypatch, admin, TimeExhausted("no receipt in 30s"))

    r = client.post(f"/markets/{mid}/redeem_position", headers=hdr(user.api_key))

    assert r.status_code == 503, r.text
    assert "not confirmed yet" in r.json()["detail"]
    # The chain paid all the same; only the answer was lost.
    (receipt,) = unseen
    assert receipt["status"] == 1
    assert admin.usd_balance(user.eth_address) == usd_before + 100_000_000
    assert admin.ctf_balances(user.eth_address, tokens) == [0, 0]
    assert pending_user_txs(db) == [(_hash(receipt), user.api_key, "REDEEM", mid, {})]
    assert tx_rows(db, user, "REDEEM") == 0

    # Not "nothing to claim" for a claim that was paid, and no second top-up.
    native = admin.native_balance(user.eth_address)
    r = client.post(
        "/positions/claim",
        json={"condition_id": market.condition_id.value},
        headers=hdr(user.api_key),
    )
    assert r.status_code == 409, r.text
    assert admin.native_balance(user.eth_address) == native

    assert reconcile_pending_user_txs(db, admin) == 1

    assert redeem_amounts(db, user) == [100_000_000]
    assert pending_user_txs(db) == []
    closed = AccountService(db, admin).list_closed_positions(user.eth_address)
    assert len(closed) == 1
    assert closed[0].curPrice == 1.0
    assert closed[0].currentValue == 100.0


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(TimeExhausted("no receipt in 30s"), id="receipt-timeout"),
        pytest.param(requests.ReadTimeout("read timed out"), id="lost-answer"),
    ],
)
def test_a_split_whose_answer_was_lost_is_not_split_twice(monkeypatch, error):
    client, admin, db = _app()
    market, _pm = synced_market(db, admin)
    mid = market.market_id
    user = onboarded_account(db, admin)
    drain_native_balance(admin, user.eth_address)
    tokens = [int(t) for t, _label in market.erc1155_tokens]
    unseen = _lose_the_answer(monkeypatch, admin, error)

    def post_split():
        return client.post(
            f"/markets/{mid}/split_position",
            json={"amount": 40_000_000},
            headers=hdr(user.api_key),
        )

    r = post_split()

    assert r.status_code == 503, r.text
    (receipt,) = unseen
    assert receipt["status"] == 1
    assert admin.ctf_balances(user.eth_address, tokens) == [40_000_000, 40_000_000]
    assert pending_user_txs(db) == [
        (_hash(receipt), user.api_key, "SPLIT", mid, {"amount": 40_000_000})
    ]
    assert tx_rows(db, user, "SPLIT") == 0

    # The client retries what it was told had failed: refused, not split again.
    r = post_split()
    assert r.status_code == 409, r.text
    assert admin.ctf_balances(user.eth_address, tokens) == [40_000_000, 40_000_000]
    assert len(unseen) == 1

    assert reconcile_pending_user_txs(db, admin) == 1

    assert tx_rows(db, user, "SPLIT") == 1
    assert pending_user_txs(db) == []
    # The split now makes them a participant, so auto-redeem will find them.
    with db.read() as conn:
        participants = TableRead.list_participant_api_keys_for_market(
            conn, mid, [t for t, _label in market.erc1155_tokens]
        )
    assert user.api_key in participants
