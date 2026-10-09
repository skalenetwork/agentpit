"""Split, merge and claim from a wallet holding no native coin, on anvil, every
transaction paid by `UserGasSponsor`'s exact top-up. The gate's branches are
pinned with fakes in tests/services/test_position_service.py."""

from contextlib import contextmanager

import pytest
from eth_account import Account
from web3 import Web3
from web3.logs import DISCARD

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
from tests.onchain import _helpers as h

admin, db = h.admin, h.db  # the shared fixtures
_PARTITION = [1, 2]


def _need(admin, fn, address) -> int:
    """What the sponsor sizes `fn` at: the estimate plus 20%, at the current price."""
    return admin.estimate_user_gas(fn, address) * 120 // 100 * admin.gas_price()


def _last_receipt(admin, address):
    """The receipt of `address`'s latest transaction (anvil mines one per block)."""
    web3 = admin._client.web3  # noqa: SLF001
    sender = Web3.to_checksum_address(address)
    head = web3.eth.block_number
    for number in range(head, max(head - 8, -1), -1):
        for tx in reversed(web3.eth.get_block(number, full_transactions=True)["transactions"]):
            if tx["from"] == sender:
                return web3.eth.get_transaction_receipt(tx["hash"])
    raise AssertionError(f"no recent transaction from {sender}")


def _spent(receipt) -> int:
    return receipt["gasUsed"] * receipt["effectiveGasPrice"]


@contextmanager
def _sponsored_from_empty(admin, db, user, call):
    """Around one sponsored `call` from an emptied wallet: topped up from 0 to exactly its
    need (the call spent part of it), the transfer plus the gas used booked."""
    h.drain_native_balance(admin, user.eth_address)
    need, booked = _need(admin, call, user.eth_address), h.sponsored_gas(db, user.api_key)
    yield
    receipt = _last_receipt(admin, user.eth_address)
    assert receipt["status"] == 1
    assert admin.native_balance(user.eth_address) + _spent(receipt) == need
    assert h.sponsored_gas(db, user.api_key) - booked == TRANSFER_GAS + receipt["gasUsed"]


def _refused_without_a_transaction(db, admin, user, market_id, error, text) -> None:
    """Claim, expect `error`, and prove nothing moved: nonces, native, booked gas, REDEEM rows."""
    wallet, payer = user.eth_address, admin.oracle_address

    def snapshot():
        return (
            admin.transaction_count(wallet),
            admin.native_balance(wallet),
            admin.transaction_count(payer),
            admin.native_balance(payer),
            h.sponsored_gas(db, user.api_key),
        )

    before = snapshot()
    with pytest.raises(error, match=text):
        h.position_service(db, admin).redeem(user, market_id)
    assert snapshot() == before
    assert h.tx_details(db, user, "REDEEM") == []


def _after_top_up(monkeypatch, admin, user, action) -> None:
    """Run `action()` once the sponsor's top-up of `user` has mined: the window where
    the world can change under a gate that passed. It must not call `admin.fund_gas`."""
    real = admin.fund_gas
    me = user.eth_address.lower()

    def fund_gas(address, value_wei, **kwargs):
        receipt = real(address, value_wei, **kwargs)
        if address.lower() == me:
            action()
        return receipt

    monkeypatch.setattr(admin, "fund_gas", fund_gas)


def test_a_claim_from_an_empty_wallet_is_topped_up_exactly_and_pays_out(admin, db):
    market, user = h.dry_winner(db, admin)
    assert admin.native_balance(user.eth_address) == 0
    redeem = admin.redeem_call(hex2bytes(market.condition_id.value), _PARTITION)
    usd_before = admin.usd_balance(user.eth_address)

    with _sponsored_from_empty(admin, db, user, redeem):
        claimed = h.position_service(db, admin).redeem(user, market.market_id)

    tokens = [int(t) for t, _label in market.erc1155_tokens]
    assert claimed.collateral_amount == 100_000_000
    assert admin.usd_balance(user.eth_address) == usd_before + 100_000_000
    assert admin.ctf_balances(user.eth_address, tokens) == [0, 0]  # the loser burned too
    assert len(h.tx_details(db, user, "REDEEM")) == 1


def test_consecutive_claims_never_leave_more_than_one_claims_need(admin, db):
    """A top-up comes only when short, and to exactly the need: the balance never creeps up."""
    user = h.onboarded_account(db, admin)
    markets = [h.synced_market(db, admin) for _ in range(3)]
    for market, _pm in markets:
        h.split(db, admin, user, market, 20_000_000)
    h.resolve_yes(db, admin, *(pm for _market, pm in markets))
    h.drain_native_balance(admin, user.eth_address)

    needs: list[int] = []
    for market, _pm in markets:
        cid = hex2bytes(market.condition_id.value)
        needs.append(_need(admin, admin.redeem_call(cid, _PARTITION), user.eth_address))
        before = admin.native_balance(user.eth_address)

        claimed = h.position_service(db, admin).redeem(user, market.market_id)

        assert claimed.collateral_amount == 20_000_000

        after = admin.native_balance(user.eth_address)
        # Topped up to exactly this claim's need, and only when short of it.
        assert after + _spent(_last_receipt(admin, user.eth_address)) == max(before, needs[-1])
        assert after <= max(needs)


@pytest.mark.parametrize(
    "gift",
    [None, (1, 50_000_000), (0, 9_999)],
    ids=["zero-holdings", "losing-tokens-only", "dust-below-a-cent"],
)
def test_nothing_worth_claiming_is_refused_without_a_transaction(admin, db, gift):
    """`redeemPositions` succeeds with nothing to redeem: ungated, each would cost a top-up."""
    market, pm = h.synced_market(db, admin)
    whale = h.onboarded_account(db, admin)
    h.split(db, admin, whale, market, 100_000_000)
    claimant = h.new_account(db)
    if gift is not None:
        index, amount = gift
        token = int(market.erc1155_tokens[index][0])
        h.give_tokens(admin, whale, claimant.eth_address, token, amount)
    h.resolve_yes(db, admin, pm)
    assert admin.native_balance(claimant.eth_address) == 0  # a claim would need a top-up

    _refused_without_a_transaction(
        db, admin, claimant, market.market_id, NothingToClaimError, "nothing to claim"
    )


def test_a_market_the_chain_has_not_resolved_is_refused_without_a_transaction(admin, db):
    """RESOLVED in the DB, no `reportPayouts` on chain: the claim would revert after the top-up."""
    market, _pm = h.synced_market(db, admin)
    user = h.onboarded_account(db, admin)
    h.split(db, admin, user, market, 100_000_000)
    with db.write() as conn:
        TableWrite.resolve_market(conn, market_id=market.market_id, winning_outcome_index=0)
    h.drain_native_balance(admin, user.eth_address)
    assert admin.payout_vector(hex2bytes(market.condition_id.value))[0] == 0

    _refused_without_a_transaction(
        db, admin, user, market.market_id, MarketStateError, "not resolved on chain"
    )


def test_split_and_merge_from_an_empty_wallet_are_topped_up_and_booked(admin, db):
    market, _pm = h.synced_market(db, admin)
    user = h.onboarded_account(db, admin)
    positions = h.position_service(db, admin)
    cid = hex2bytes(market.condition_id.value)
    tokens = [int(t) for t, _label in market.erc1155_tokens]

    with _sponsored_from_empty(admin, db, user, admin.split_call(cid, _PARTITION, 40_000_000)):
        positions.split(user, market.market_id, SplitPositionRequest(amount=40_000_000))
    assert admin.ctf_balances(user.eth_address, tokens) == [40_000_000, 40_000_000]

    with _sponsored_from_empty(admin, db, user, admin.merge_call(cid, _PARTITION, 15_000_000)):
        positions.merge(user, market.market_id, MergePositionRequest(amount=15_000_000))
    assert admin.ctf_balances(user.eth_address, tokens) == [25_000_000, 25_000_000]

    assert len(h.tx_details(db, user, "SPLIT")) == 1
    assert len(h.tx_details(db, user, "MERGE")) == 1


def test_with_the_kill_switch_off_a_dry_wallet_gets_402_not_500():
    """AGENTPIT_SPONSOR_USER_GAS=false: the node refuses the dry claim; the caller gets 402."""
    client, admin, db = h.app_world(Settings(sponsor_user_gas=False))
    market, user = h.dry_winner(db, admin)  # set up with the environment's sponsor
    tokens = [int(t) for t, _label in market.erc1155_tokens]
    nonce, booked = admin.transaction_count(user.eth_address), h.sponsored_gas(db, user.api_key)

    r = client.post(f"/markets/{market.market_id}/redeem_position", headers=h.hdr(user.api_key))

    assert r.status_code == 402, r.text
    assert admin.native_balance(user.eth_address) == 0  # nothing was topped up
    assert admin.transaction_count(user.eth_address) == nonce  # the node took nothing
    assert admin.ctf_balances(user.eth_address, tokens) == [100_000_000, 100_000_000]
    assert h.sponsored_gas(db, user.api_key) == booked  # the switch stops booking as well
    assert h.tx_details(db, user, "REDEEM") == []


@pytest.mark.parametrize(
    ("credit", "debit"),
    [(7_000_000, 0), (0, 130_000_000)],
    ids=["a-mint-of-7-lands-during-the-claim", "a-transfer-of-130-leaves-during-the-claim"],
)
def test_a_claim_is_logged_at_the_ctf_payout_whatever_the_wallet_does_meanwhile(
    admin, db, monkeypatch, credit, debit
):
    """A balance diff read 107 or -30 (dropped as a loss): log the receipt's payout instead."""
    market, user = h.dry_winner(db, admin)  # a top-up is certain
    elsewhere = Account.create().address

    def moves_apusd():
        if credit:
            admin.mint_to(user.eth_address, credit)
        if debit:
            transfer = admin._contracts.usd.functions.transfer(elsewhere, debit)  # noqa: SLF001
            h.send_as(admin, user, transfer)

    _after_top_up(monkeypatch, admin, user, moves_apusd)

    claimed = h.position_service(db, admin).redeem(user, market.market_id)

    receipt = _last_receipt(admin, user.eth_address)
    assert receipt["status"] == 1
    paid = admin._contracts.ctf.events.PayoutRedemption().process_receipt(  # noqa: SLF001
        receipt, errors=DISCARD
    )[0]["args"]["payout"]
    assert paid == 100_000_000
    assert claimed.collateral_amount == paid
    assert [d["collateral_amount"] for d in h.tx_details(db, user, "REDEEM")] == [paid]
    assert claimed.new_usdc_balance == admin.usd_balance(user.eth_address)

    closed = AccountService(db, admin).list_closed_positions(user.eth_address)
    assert len(closed) == 1
    assert closed[0].curPrice == 1.0
    assert closed[0].currentValue == 100.0


def test_a_split_on_a_market_that_resolves_during_its_top_up_is_refused(admin, db, monkeypatch):
    """Resolved during the top-up, a split would mint a claimable winner: refused unsigned."""
    market, pm = h.synced_market(db, admin)
    user = h.onboarded_account(db, admin)
    tokens = [int(t) for t, _label in market.erc1155_tokens]
    h.drain_native_balance(admin, user.eth_address)
    usd = admin.usd_balance(user.eth_address)
    nonce, booked = admin.transaction_count(user.eth_address), h.sponsored_gas(db, user.api_key)
    _after_top_up(monkeypatch, admin, user, lambda: h.resolve_yes(db, admin, pm))

    with pytest.raises(MarketStateError, match="split only runs on ACTIVE markets"):
        h.position_service(db, admin).split(
            user, market.market_id, SplitPositionRequest(amount=40_000_000)
        )

    assert admin.ctf_balances(user.eth_address, tokens) == [0, 0]
    assert admin.usd_balance(user.eth_address) == usd
    assert admin.transaction_count(user.eth_address) == nonce  # nothing was signed
    assert h.tx_details(db, user, "SPLIT") == []
    assert h.pending_user_txs(db) == []
    assert h.sponsored_gas(db, user.api_key) - booked == TRANSFER_GAS


def test_a_claim_whose_tokens_left_during_its_top_up_is_refused(admin, db, monkeypatch):
    """The winners leave during the top-up, as a filled SELL would move them: never signed."""
    market, user = h.dry_winner(db, admin)
    tokens = [int(t) for t, _label in market.erc1155_tokens]
    nonce, booked = admin.transaction_count(user.eth_address), h.sponsored_gas(db, user.api_key)

    def winners_leave():
        h.give_tokens(admin, user, Account.create().address, tokens[0], 100_000_000)

    _after_top_up(monkeypatch, admin, user, winners_leave)

    with pytest.raises(NothingToClaimError, match="nothing to claim"):
        h.position_service(db, admin).redeem(user, market.market_id)

    assert admin.ctf_balances(user.eth_address, tokens) == [0, 100_000_000]
    assert admin.transaction_count(user.eth_address) == nonce + 1  # the transfer only
    assert h.tx_details(db, user, "REDEEM") == []
    assert h.pending_user_txs(db) == []
    assert h.sponsored_gas(db, user.api_key) - booked == TRANSFER_GAS


def test_a_claim_that_mines_with_no_payout_writes_no_row(admin, db, monkeypatch):
    """Gate and re-check pass (balances faked), the claim mines for nothing: gas booked, no row."""
    market, pm = h.synced_market(db, admin)
    user = h.onboarded_account(db, admin)  # holds no outcome token at all
    h.resolve_yes(db, admin, pm)
    h.drain_native_balance(admin, user.eth_address)
    me, real = user.eth_address.lower(), admin.ctf_balances
    monkeypatch.setattr(
        admin,
        "ctf_balances",
        lambda address, ids: [100_000_000, 0] if address.lower() == me else real(address, ids),
    )
    booked = h.sponsored_gas(db, user.api_key)

    with pytest.raises(NothingToClaimError):
        h.position_service(db, admin).redeem(user, market.market_id)

    receipt = _last_receipt(admin, user.eth_address)
    assert receipt["status"] == 1  # it mined, and paid nothing
    assert h.sponsored_gas(db, user.api_key) - booked == TRANSFER_GAS + receipt["gasUsed"]
    assert h.tx_details(db, user, "REDEEM") == []
    assert h.pending_user_txs(db) == []
