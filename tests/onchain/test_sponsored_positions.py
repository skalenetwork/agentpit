"""Split, merge and claim from a wallet holding no native coin, on anvil.

`UserGasSponsor` pays for every user transaction: just before the call, the
admin sends the wallet exactly what it needs. Each branch of the gate is pinned
with fakes in tests/services/test_position_service.py.
"""

from __future__ import annotations

import pytest
from eth_account import Account
from fastapi.testclient import TestClient
from web3 import Web3
from web3.logs import DISCARD

from agentpit.api.app import create_app
from agentpit.api.deps import get_db_session, get_onchain_admin
from agentpit.config import Settings
from agentpit.datastructures.split_position_request import (
    MergePositionRequest,
    SplitPositionRequest,
)
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import MarketStateError, NothingToClaimError
from agentpit.onchain.tx_sender import TRANSFER_GAS
from agentpit.services.account_service import AccountService
from agentpit.utils.parse import hex2bytes
from tests.onchain._helpers import (
    chain,
    drain_native_balance,
    dry_winner,
    give_tokens,
    hdr,
    new_account,
    onboarded_account,
    pending_user_txs,
    position_service,
    redeem_amounts,
    resolve_yes,
    send_as,
    split,
    sponsored_gas,
    synced_market,
    tx_rows,
)

_PARTITION = [1, 2]


def _need(admin, fn, address) -> int:
    """What the sponsor sizes `fn` at: the fee-less estimate plus 20%, at the
    current gas price. Exact when nothing is mined between this and the send."""
    return admin.estimate_user_gas(fn, address) * 120 // 100 * admin.gas_price()


def _last_receipt(admin, address):
    """The receipt of `address`'s latest transaction (anvil mines one per
    block, so it is in one of the last few)."""
    web3 = admin._client.web3  # noqa: SLF001
    sender = Web3.to_checksum_address(address)
    head = web3.eth.block_number
    for number in range(head, max(head - 8, -1), -1):
        block = web3.eth.get_block(number, full_transactions=True)
        for tx in reversed(block["transactions"]):
            if tx["from"] == sender:
                return web3.eth.get_transaction_receipt(tx["hash"])
    raise AssertionError(f"no recent transaction from {sender}")


def _spent(receipt) -> int:
    return receipt["gasUsed"] * receipt["effectiveGasPrice"]


def _assert_topped_up_to(admin, db, user, need: int, booked_before: int) -> None:
    """The last transaction mined, the wallet was topped up from 0 to exactly
    `need` (the transaction spent part of it), and the top-up's transfer plus
    the transaction's gas are booked: never refused for budget, always counted."""
    receipt = _last_receipt(admin, user.eth_address)
    assert receipt["status"] == 1
    assert admin.native_balance(user.eth_address) + _spent(receipt) == need
    assert sponsored_gas(db, user) - booked_before == TRANSFER_GAS + receipt["gasUsed"]


def _refused_without_a_transaction(db, admin, user, market_id, error, text) -> None:
    """Claim, expect `error`, and prove nothing was sent: neither the user's
    nonce nor the admin's moved, neither balance changed, nothing was booked,
    no REDEEM row was written."""
    wallet, payer = user.eth_address, admin.oracle_address

    def snapshot():
        return (
            admin.transaction_count(wallet),
            admin.native_balance(wallet),
            admin.transaction_count(payer),
            admin.native_balance(payer),
            sponsored_gas(db, user),
        )

    before = snapshot()
    with pytest.raises(error, match=text):
        position_service(db, admin).redeem(user, market_id)
    assert snapshot() == before
    assert tx_rows(db, user, "REDEEM") == 0


def _after_top_up(monkeypatch, admin, user, action) -> None:
    """Run `action()` once the sponsor's top-up of `user` has mined: the window
    between the gate and the user's transaction, in which the world can change
    (a market resolves, a resting SELL fills, a fill or `/me/top-up` moves apUSD
    without taking the lock). `action` must not call `admin.fund_gas`: that is
    this hook."""
    real = admin.fund_gas
    me = user.eth_address.lower()

    def fund_gas(address, value_wei, **kwargs):
        receipt = real(address, value_wei, **kwargs)
        if address.lower() == me:
            action()
        return receipt

    monkeypatch.setattr(admin, "fund_gas", fund_gas)


def test_a_claim_from_an_empty_wallet_is_topped_up_exactly_and_pays_out():
    admin, db = chain()
    market, user = dry_winner(db, admin)
    assert admin.native_balance(user.eth_address) == 0
    cid = hex2bytes(market.condition_id.value)
    need = _need(admin, admin.redeem_call(cid, _PARTITION), user.eth_address)
    usd_before, booked_before = admin.usd_balance(user.eth_address), sponsored_gas(db, user)

    claimed = position_service(db, admin).redeem(user, market.market_id)

    tokens = [int(t) for t, _label in market.erc1155_tokens]
    assert claimed.collateral_amount == 100_000_000
    assert admin.usd_balance(user.eth_address) == usd_before + 100_000_000
    assert admin.ctf_balances(user.eth_address, tokens) == [0, 0]  # the loser burned too
    _assert_topped_up_to(admin, db, user, need, booked_before)
    assert tx_rows(db, user, "REDEEM") == 1


def test_consecutive_claims_never_leave_more_than_one_claims_need():
    """A top-up happens only when the wallet holds less than the next claim
    needs, and brings it to exactly that, so the balance cannot creep: after
    each claim it holds at most the largest need so far (on anvil, almost all
    of it, because anvil bills the base fee, not maxFeePerGas)."""
    admin, db = chain()
    user = onboarded_account(db, admin)
    markets = [synced_market(db, admin) for _ in range(3)]
    for market, _pm in markets:
        split(db, admin, user, market, 20_000_000)
    resolve_yes(db, admin, *(pm for _market, pm in markets))
    drain_native_balance(admin, user.eth_address)

    needs: list[int] = []
    for market, _pm in markets:
        cid = hex2bytes(market.condition_id.value)
        needs.append(_need(admin, admin.redeem_call(cid, _PARTITION), user.eth_address))
        before = admin.native_balance(user.eth_address)

        assert position_service(db, admin).redeem(user, market.market_id).collateral_amount == 20_000_000

        after = admin.native_balance(user.eth_address)
        # Topped up to exactly this claim's need, and only when short of it.
        assert after + _spent(_last_receipt(admin, user.eth_address)) == max(before, needs[-1])
        assert after <= max(needs)


@pytest.mark.parametrize(
    "gift",
    [
        pytest.param(None, id="zero-holdings"),
        pytest.param((1, 50_000_000), id="losing-tokens-only"),
        pytest.param((0, 9_999), id="dust-below-a-cent"),
    ],
)
def test_nothing_worth_claiming_is_refused_without_a_transaction(gift):
    """`redeemPositions` succeeds with nothing to redeem, so without the gate
    each of these would cost the admin a top-up and the account a claim."""
    admin, db = chain()
    market, pm = synced_market(db, admin)
    whale = onboarded_account(db, admin)
    split(db, admin, whale, market, 100_000_000)
    claimant = new_account(db)
    if gift is not None:
        index, amount = gift
        give_tokens(admin, whale, claimant.eth_address, int(market.erc1155_tokens[index][0]), amount)
    resolve_yes(db, admin, pm)
    assert admin.native_balance(claimant.eth_address) == 0  # a claim would need a top-up

    _refused_without_a_transaction(
        db, admin, claimant, market.market_id, NothingToClaimError, "nothing to claim"
    )


def test_a_market_the_chain_has_not_resolved_is_refused_without_a_transaction():
    """RESOLVED in the database with no `reportPayouts` on chain:
    `redeemPositions` would revert, after the admin had paid for the top-up."""
    admin, db = chain()
    market, _pm = synced_market(db, admin)
    user = onboarded_account(db, admin)
    split(db, admin, user, market, 100_000_000)
    with db.write() as conn:
        TableWrite.resolve_market(conn, market_id=market.market_id, winning_outcome_index=0)
    drain_native_balance(admin, user.eth_address)
    assert admin.payout_vector(hex2bytes(market.condition_id.value))[0] == 0

    _refused_without_a_transaction(
        db, admin, user, market.market_id, MarketStateError, "not resolved on chain"
    )


def test_split_and_merge_from_an_empty_wallet_are_topped_up_and_booked():
    admin, db = chain()
    market, _pm = synced_market(db, admin)
    user = onboarded_account(db, admin)
    positions = position_service(db, admin)
    cid = hex2bytes(market.condition_id.value)
    tokens = [int(t) for t, _label in market.erc1155_tokens]

    drain_native_balance(admin, user.eth_address)
    need = _need(admin, admin.split_call(cid, _PARTITION, 40_000_000), user.eth_address)
    booked = sponsored_gas(db, user)
    positions.split(user, market.market_id, SplitPositionRequest(amount=40_000_000))
    _assert_topped_up_to(admin, db, user, need, booked)  # the reservation trued up to the spend
    assert admin.ctf_balances(user.eth_address, tokens) == [40_000_000, 40_000_000]

    drain_native_balance(admin, user.eth_address)
    need = _need(admin, admin.merge_call(cid, _PARTITION, 15_000_000), user.eth_address)
    booked = sponsored_gas(db, user)
    positions.merge(user, market.market_id, MergePositionRequest(amount=15_000_000))
    _assert_topped_up_to(admin, db, user, need, booked)
    assert admin.ctf_balances(user.eth_address, tokens) == [25_000_000, 25_000_000]

    assert tx_rows(db, user, "SPLIT") == 1
    assert tx_rows(db, user, "MERGE") == 1


def test_with_the_kill_switch_off_a_dry_wallet_gets_402_not_500():
    """AGENTPIT_SPONSOR_USER_GAS=false: nothing is topped up, so the node
    refuses the claim for want of gas. The caller gets 402, "the wallet could
    not pay", not a 500 from a raw RPC error."""
    client = TestClient(
        create_app(Settings(sponsor_user_gas=False)), raise_server_exceptions=False
    )
    admin = client.app.dependency_overrides[get_onchain_admin]()  # type: ignore[attr-defined]
    db = client.app.dependency_overrides[get_db_session]()  # type: ignore[attr-defined]
    market, user = dry_winner(db, admin)  # set up with the environment's sponsor
    tokens = [int(t) for t, _label in market.erc1155_tokens]
    nonce, booked = admin.transaction_count(user.eth_address), sponsored_gas(db, user)

    r = client.post(
        f"/markets/{market.market_id}/redeem_position", headers=hdr(user.api_key)
    )

    assert r.status_code == 402, r.text
    assert admin.native_balance(user.eth_address) == 0  # nothing was topped up
    assert admin.transaction_count(user.eth_address) == nonce  # the node took nothing
    assert admin.ctf_balances(user.eth_address, tokens) == [100_000_000, 100_000_000]
    assert sponsored_gas(db, user) == booked  # the switch stops booking as well
    assert tx_rows(db, user, "REDEEM") == 0


@pytest.mark.parametrize(
    ("credit", "debit"),
    [
        pytest.param(7_000_000, 0, id="a-mint-of-7-lands-during-the-claim"),
        pytest.param(0, 130_000_000, id="a-transfer-of-130-leaves-during-the-claim"),
    ],
)
def test_a_claim_is_logged_at_the_ctf_payout_whatever_the_wallet_does_meanwhile(
    monkeypatch, credit, debit
):
    """A difference of two balance reads was wrong in both directions: a credit
    during the claim made it read 107, and a debit larger than the payout made
    it read -30, which `list_closed_positions` takes for a lost market and
    drops. The amount is what `redeemPositions` paid, from the receipt."""
    admin, db = chain()
    market, user = dry_winner(db, admin)  # a top-up is certain
    elsewhere = Account.create().address

    def moves_apusd():
        if credit:
            admin.mint_to(user.eth_address, credit)
        if debit:
            send_as(admin, user, admin._contracts.usd.functions.transfer(elsewhere, debit))  # noqa: SLF001

    _after_top_up(monkeypatch, admin, user, moves_apusd)

    claimed = position_service(db, admin).redeem(user, market.market_id)

    receipt = _last_receipt(admin, user.eth_address)
    assert receipt["status"] == 1
    paid = admin._contracts.ctf.events.PayoutRedemption().process_receipt(  # noqa: SLF001
        receipt, errors=DISCARD
    )[0]["args"]["payout"]
    assert paid == 100_000_000
    assert claimed.collateral_amount == paid
    assert redeem_amounts(db, user) == [paid]
    assert claimed.new_usdc_balance == admin.usd_balance(user.eth_address)

    closed = AccountService(db, admin).list_closed_positions(user.eth_address)
    assert len(closed) == 1
    assert closed[0].curPrice == 1.0
    assert closed[0].currentValue == 100.0


def test_a_split_on_a_market_that_resolves_during_its_top_up_is_refused(monkeypatch):
    """`split` checks the market before the lock, and the top-up takes a block
    or several on SKALE. Resolved in that window, the market would take a split
    that mints a claimable winner. The sponsor re-reads the market once the
    wallet is funded: nothing is signed, the apUSD stays, and only the top-up's
    transfer is booked, the reservation having gone back."""
    admin, db = chain()
    market, pm = synced_market(db, admin)
    user = onboarded_account(db, admin)
    tokens = [int(t) for t, _label in market.erc1155_tokens]
    drain_native_balance(admin, user.eth_address)
    usd = admin.usd_balance(user.eth_address)
    nonce, booked = admin.transaction_count(user.eth_address), sponsored_gas(db, user)
    _after_top_up(monkeypatch, admin, user, lambda: resolve_yes(db, admin, pm))

    with pytest.raises(MarketStateError, match="split only runs on ACTIVE markets"):
        position_service(db, admin).split(
            user, market.market_id, SplitPositionRequest(amount=40_000_000)
        )

    assert admin.ctf_balances(user.eth_address, tokens) == [0, 0]
    assert admin.usd_balance(user.eth_address) == usd
    assert admin.transaction_count(user.eth_address) == nonce  # nothing was signed
    assert tx_rows(db, user, "SPLIT") == 0
    assert pending_user_txs(db) == []
    assert sponsored_gas(db, user) - booked == TRANSFER_GAS


def test_a_claim_whose_tokens_left_during_its_top_up_is_refused(monkeypatch):
    """A resting SELL filled by the admin's `matchOrders` moves the winning
    tokens with no transaction of the user's, and the claim's top-up is a
    window in which that can happen. Here the user sends them away itself.
    The claim, which `redeemPositions` would let mine at a payout of nothing,
    is refused with the gate's own error and never signed."""
    admin, db = chain()
    market, user = dry_winner(db, admin)
    tokens = [int(t) for t, _label in market.erc1155_tokens]
    nonce, booked = admin.transaction_count(user.eth_address), sponsored_gas(db, user)
    _after_top_up(
        monkeypatch,
        admin,
        user,
        lambda: give_tokens(admin, user, Account.create().address, tokens[0], 100_000_000),
    )

    with pytest.raises(NothingToClaimError, match="nothing to claim"):
        position_service(db, admin).redeem(user, market.market_id)

    assert admin.ctf_balances(user.eth_address, tokens) == [0, 100_000_000]
    assert admin.transaction_count(user.eth_address) == nonce + 1  # the transfer only
    assert tx_rows(db, user, "REDEEM") == 0
    assert pending_user_txs(db) == []
    assert sponsored_gas(db, user) - booked == TRANSFER_GAS


def test_a_claim_that_mines_with_no_payout_writes_no_row(monkeypatch):
    """The gate and the re-check both pass (the balances are read through a
    lie here, because nothing real can empty the wallet between the re-check
    and the block), and the claim mines for a payout of nothing. The admin
    paid for it, so its gas is booked, but the account gets no REDEEM row, no
    intent row stays, and the caller hears `NothingToClaimError`."""
    admin, db = chain()
    market, pm = synced_market(db, admin)
    user = onboarded_account(db, admin)  # holds no outcome token at all
    resolve_yes(db, admin, pm)
    drain_native_balance(admin, user.eth_address)
    me, real = user.eth_address.lower(), admin.ctf_balances
    monkeypatch.setattr(
        admin,
        "ctf_balances",
        lambda address, ids: [100_000_000, 0] if address.lower() == me else real(address, ids),
    )
    booked = sponsored_gas(db, user)

    with pytest.raises(NothingToClaimError):
        position_service(db, admin).redeem(user, market.market_id)

    receipt = _last_receipt(admin, user.eth_address)
    assert receipt["status"] == 1  # it mined, and paid nothing
    assert sponsored_gas(db, user) - booked == TRANSFER_GAS + receipt["gasUsed"]
    assert tx_rows(db, user, "REDEEM") == 0
    assert pending_user_txs(db) == []
