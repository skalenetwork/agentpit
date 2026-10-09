"""A split or claim that mined after we stopped waiting for it, on anvil.

`send_as_user` sends for real, then raises as if the answer had been lost. The caller
gets 503 "not confirmed yet" and the intent row stays; a retry is a 409 (not a second
split, nor "nothing to claim" for a paid claim); `reconcile_pending_user_txs` writes the
history row at the exact payout. Unit cases: tests/services/test_position_service.py.
"""

import pytest
import requests
from web3.exceptions import TimeExhausted

from agentpit.db.table_read import TableRead
from agentpit.services.account_service import AccountService
from agentpit.services.pending_user_txs import reconcile_pending_user_txs
from tests.onchain import _helpers as h


def _lose_the_answer(monkeypatch, admin, error: Exception) -> list:
    """Send every user transaction for real, then raise `error` in place of its receipt."""
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
    client, admin, db = h.app_world()
    market, user = h.dry_winner(db, admin)
    mid = market.market_id
    tokens = [int(t) for t, _label in market.erc1155_tokens]
    usd_before = admin.usd_balance(user.eth_address)
    unseen = _lose_the_answer(monkeypatch, admin, TimeExhausted("no receipt in 30s"))

    r = client.post(f"/markets/{mid}/redeem_position", headers=h.hdr(user.api_key))

    assert r.status_code == 503, r.text
    assert "not confirmed yet" in r.json()["detail"]
    (receipt,) = unseen  # the chain paid all the same; only the answer was lost
    assert receipt["status"] == 1
    assert admin.usd_balance(user.eth_address) == usd_before + 100_000_000
    assert admin.ctf_balances(user.eth_address, tokens) == [0, 0]
    assert h.pending_user_txs(db) == [(_hash(receipt), user.api_key, "REDEEM", mid, {})]
    assert h.tx_details(db, user, "REDEEM") == []

    # Not "nothing to claim" for a claim that was paid, and no second top-up.
    native = admin.native_balance(user.eth_address)
    r = client.post(
        "/positions/claim",
        json={"condition_id": market.condition_id.value},
        headers=h.hdr(user.api_key),
    )
    assert r.status_code == 409, r.text
    assert admin.native_balance(user.eth_address) == native

    assert reconcile_pending_user_txs(db, admin) == 1

    assert [d["collateral_amount"] for d in h.tx_details(db, user, "REDEEM")] == [100_000_000]
    assert h.pending_user_txs(db) == []
    closed = AccountService(db, admin).list_closed_positions(user.eth_address)
    assert len(closed) == 1
    assert closed[0].curPrice == 1.0
    assert closed[0].currentValue == 100.0


@pytest.mark.parametrize(
    "error",
    [TimeExhausted("no receipt in 30s"), requests.ReadTimeout("read timed out")],
    ids=["receipt-timeout", "lost-answer"],
)
def test_a_split_whose_answer_was_lost_is_not_split_twice(monkeypatch, error):
    client, admin, db = h.app_world()
    market = h.synced_market(db, admin)
    mid = market.market_id
    user = h.onboarded_account(db, admin)
    h.drain_native_balance(admin, user.eth_address)
    tokens = [int(t) for t, _label in market.erc1155_tokens]
    unseen = _lose_the_answer(monkeypatch, admin, error)

    def post_split():
        url = f"/markets/{mid}/split_position"
        return client.post(url, json={"amount": 40_000_000}, headers=h.hdr(user.api_key))

    r = post_split()

    assert r.status_code == 503, r.text
    (receipt,) = unseen
    assert receipt["status"] == 1
    assert admin.ctf_balances(user.eth_address, tokens) == [40_000_000, 40_000_000]
    pending = (_hash(receipt), user.api_key, "SPLIT", mid, {"amount": 40_000_000})
    assert h.pending_user_txs(db) == [pending]
    assert h.tx_details(db, user, "SPLIT") == []

    # The client retries what it was told had failed: refused, not split again.
    r = post_split()
    assert r.status_code == 409, r.text
    assert admin.ctf_balances(user.eth_address, tokens) == [40_000_000, 40_000_000]
    assert len(unseen) == 1

    assert reconcile_pending_user_txs(db, admin) == 1

    assert len(h.tx_details(db, user, "SPLIT")) == 1
    assert h.pending_user_txs(db) == []
    # The split now makes them a participant, so auto-redeem will find them.
    with db.read() as conn:
        participants = TableRead.list_participant_api_keys_for_market(
            conn, mid, [t for t, _label in market.erc1155_tokens]
        )
    assert user.api_key in participants
