"""What `UserGasSponsor` is built from, against the real contracts: the user-signed
call builders on `OnchainAdmin`, and the reads and send it sizes a top-up with.
Its promise is "top up to exactly what the calls need, then send"; these pin the facts it stands on.
"""

import secrets
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from eth_account import Account
from web3.exceptions import Web3RPCError

from agentpit.onchain.admin import _MAX_UINT256, OnchainAdmin
from agentpit.onchain.chain_rpc import is_balance_low
from agentpit.onchain.ctf_ids import binary_market_ids
from tests.onchain import _helpers as h

HOLDER = "0x9D22FB092E79515611e8380026583929F88815b9"


admin = h.admin  # the shared fixture


def _condition(admin: OnchainAdmin) -> tuple[bytes, bytes, list[int]]:
    """A fresh binary condition with the admin as oracle: (question id,
    condition id, [token of index set 1, token of index set 2])."""
    question_id = secrets.token_bytes(32)
    admin.prepare_condition(admin.oracle_address, question_id, 2)
    condition_id, tokens = binary_market_ids(
        admin.oracle_address, admin.collateral_address, question_id
    )
    return question_id, condition_id, tokens


def _sized(admin: OnchainAdmin, fn, address: str) -> int:
    """The sponsor's gas limit for one call: the estimate plus 20%."""
    return admin.estimate_user_gas(fn, address) * 120 // 100


def _staged(admin: OnchainAdmin, wallet=None, *, short: int = 0):
    """`(wallet, fn, gas, price)` for the first approval call, the wallet funded
    to exactly gas x price, less `short` wei."""
    wallet = wallet or Account.create()
    fn = admin.approval_calls()[0]
    gas, price = _sized(admin, fn, wallet.address), admin.gas_price()
    admin.fund_gas(wallet.address, gas * price - short)
    return wallet, fn, gas, price


def _self_funded_house(admin: OnchainAdmin, *, dripped: bool = False):
    """A wallet that holds its own gas (and, `dripped`, apUSD with every approval set)."""
    house = Account.create()
    admin.fund_gas(house.address, 10**16)
    if dripped:
        admin.faucet_drip(house.address)
        admin.grant_user_approvals(house)
    return house


def test_an_unreported_condition_reads_no_numerators():
    ctf = MagicMock()
    ctf.functions.payoutDenominator.return_value.call.return_value = 0
    admin = OnchainAdmin(client=None, contracts=SimpleNamespace(ctf=ctf))  # type: ignore[arg-type]

    assert admin.payout_vector(b"\x11" * 32) == (0, [0, 0])
    assert admin.payout_vector(b"\x11" * 32, outcome_count=3) == (0, [0, 0, 0])
    ctf.functions.payoutNumerators.assert_not_called()  # all zero: a round trip for nothing


def test_an_estimate_carries_no_fee_fields():
    """A gasPrice/maxFeePerGas in the estimate makes anvil refuse the (empty) sponsored wallet."""
    fn = MagicMock()
    fn.estimate_gas.return_value = 46_487
    admin = OnchainAdmin(client=None, contracts=None)  # type: ignore[arg-type]

    assert admin.estimate_user_gas(fn, HOLDER.lower()) == 46_487
    fn.estimate_gas.assert_called_once_with({"from": HOLDER})


@pytest.mark.parametrize(
    ("payouts", "expected"),
    # The denominator is the numerators' sum: each side of a 50/50 pays half.
    [([1, 0], (1, [1, 0])), ([0, 1], (1, [0, 1])), ([1, 1], (2, [1, 1]))],
    ids=["yes-wins", "no-wins", "split-resolution"],
)
def test_payout_vector_before_and_after_report_payouts(admin, payouts, expected):
    question_id, condition_id, _tokens = _condition(admin)
    assert admin.payout_vector(condition_id) == (0, [0, 0])

    admin.report_payouts(question_id, payouts)
    assert admin.payout_vector(condition_id) == expected


def test_gas_price_is_the_nodes_current_price(admin):
    assert admin.gas_price() == admin._client.web3.eth.gas_price  # noqa: SLF001


def test_an_empty_wallet_can_be_estimated(admin):
    wallet = Account.create()
    assert admin._client.web3.eth.get_balance(wallet.address) == 0  # noqa: SLF001

    # Measured on anvil 2026-10-08: 46,487 and 45,996.
    for fn in admin.approval_calls():
        assert 21_000 < admin.estimate_user_gas(fn, wallet.address) < 100_000


def test_a_claim_with_nothing_to_claim_still_estimates(admin):
    """`redeemPositions` succeeds with zero holdings: the sponsor must gate a worthless claim."""
    question_id, condition_id, _tokens = _condition(admin)
    admin.report_payouts(question_id, [1, 0])

    claim = admin.redeem_call(condition_id, [1, 2])
    assert admin.estimate_user_gas(claim, Account.create().address) > 21_000


def test_transaction_count_counts_only_what_the_wallet_sent(admin):
    wallet = Account.create()
    assert admin.transaction_count(wallet.address) == 0

    _wallet, fn, gas, price = _staged(admin, wallet)
    # A top-up is a transaction *to* the wallet: its own count stays 0, which
    # tells a wiped chain from a wallet spent down to nothing.
    assert admin.transaction_count(wallet.address) == 0

    admin.send_as_user(wallet, fn, gas=gas, max_fee=price)
    assert admin.transaction_count(wallet.address) == 1
    assert admin.transaction_count(wallet.address.lower()) == 1


def test_send_as_user_sends_exactly_the_gas_and_price_it_is_given(admin):
    w3 = admin._client.web3  # noqa: SLF001
    wallet = Account.create()
    fn = admin.approval_calls()[0]
    # Not estimate x 1.2: had send_user_tx estimated again, the mined limit would differ.
    gas = admin.estimate_user_gas(fn, wallet.address) + 12_345
    price = admin.gas_price()
    admin.fund_gas(wallet.address, gas * price)  # exact: not one wei over

    receipt = admin.send_as_user(wallet, fn, gas=gas, max_fee=price)

    assert receipt["status"] == 1
    tx = w3.eth.get_transaction(receipt["transactionHash"])
    assert tx["gas"] == gas
    assert tx["maxFeePerGas"] == price
    assert tx["maxPriorityFeePerGas"] == 0
    assert w3.eth.get_balance(wallet.address) <= gas * price


def test_one_wei_short_of_gas_times_price_is_a_balance_refusal(admin):
    wallet, fn, gas, price = _staged(admin, short=1)

    with pytest.raises(Web3RPCError) as caught:
        admin.send_as_user(wallet, fn, gas=gas, max_fee=price)
    # anvil's wording ("Insufficient funds for gas * price + value"); the
    # sponsor resizes and retries once on exactly this.
    assert is_balance_low(caught.value)
    assert admin.transaction_count(wallet.address) == 0


def test_a_reverted_call_is_mined_and_returned(admin):
    """Given a limit nothing is estimated: a rejected call is mined, paid for, returned status 0."""
    _question_id, condition_id, _tokens = _condition(admin)  # not reported
    wallet = Account.create()
    gas, price = 100_000, admin.gas_price()
    admin.fund_gas(wallet.address, gas * price)

    redeem = admin.redeem_call(condition_id, [1, 2])

    receipt = admin.send_as_user(wallet, redeem, gas=gas, max_fee=price)

    assert receipt["status"] == 0
    assert receipt["gasUsed"] > 0
    assert admin.transaction_count(wallet.address) == 1


def test_approval_calls_are_the_three_onboarding_approvals_in_order(admin):
    c = admin._contracts  # noqa: SLF001

    assert [(fn.address, fn.fn_name, fn.args) for fn in admin.approval_calls()] == [
        (c.usd.address, "approve", (c.exchange.address, _MAX_UINT256)),
        (c.usd.address, "approve", (c.ctf.address, _MAX_UINT256)),
        (c.ctf.address, "setApprovalForAll", (c.exchange.address, True)),
    ]


def test_one_exact_top_up_pays_for_all_three_approvals(admin):
    """Onboarding without a grant: one top-up sized for all three calls, sent back to back."""
    wallet = Account.create()
    calls = admin.approval_calls()
    limits = [_sized(admin, fn, wallet.address) for fn in calls]
    price = admin.gas_price()
    need = sum(limits) * price
    admin.fund_gas(wallet.address, need)

    receipts = [
        admin.send_as_user(wallet, fn, gas=gas, max_fee=price) for fn, gas in zip(calls, limits)
    ]

    assert [r["status"] for r in receipts] == [1, 1, 1]
    h.assert_approvals_set(admin, wallet.address)
    assert admin.native_balance(wallet.address) <= need


def test_grant_user_approvals_still_sets_every_approval(admin):
    """The house path: a wallet that holds its own gas signs all three."""
    house = _self_funded_house(admin)

    receipts = admin.grant_user_approvals(house)

    assert [r["status"] for r in receipts] == [1, 1, 1]
    h.assert_approvals_set(admin, house.address)


def test_split_merge_and_redeem_calls_move_collateral_and_tokens(admin):
    question_id, condition_id, tokens = _condition(admin)
    wallet = _self_funded_house(admin, dripped=True)
    usd_start = admin.usd_balance(wallet.address)

    def send(fn):
        gas = _sized(admin, fn, wallet.address)
        assert admin.send_as_user(wallet, fn, gas=gas, max_fee=admin.gas_price())["status"] == 1

    split = admin.split_call(condition_id, [1, 2], 5_000_000)
    assert split.args == (admin.collateral_address, b"\x00" * 32, condition_id, [1, 2], 5_000_000)
    send(split)
    assert admin.ctf_balances(wallet.address, tokens) == [5_000_000, 5_000_000]
    assert admin.usd_balance(wallet.address) == usd_start - 5_000_000

    send(admin.merge_call(condition_id, [1, 2], 2_000_000))
    assert admin.ctf_balances(wallet.address, tokens) == [3_000_000, 3_000_000]
    assert admin.usd_balance(wallet.address) == usd_start - 3_000_000

    admin.report_payouts(question_id, [1, 0])  # index set 1 wins
    send(admin.redeem_call(condition_id, [1, 2]))
    # The winner pays 1:1, and the loser burns in the same transaction.
    assert admin.ctf_balances(wallet.address, tokens) == [0, 0]
    assert admin.usd_balance(wallet.address) == usd_start


def test_user_split_position_still_splits_for_a_self_funded_wallet(admin):
    _question_id, condition_id, tokens = _condition(admin)
    house = _self_funded_house(admin, dripped=True)

    receipt = admin.user_split_position(house, condition_id, 7_000_000)

    assert receipt["status"] == 1
    assert admin.ctf_balances(house.address, tokens) == [7_000_000, 7_000_000]


def test_the_hash_is_handed_over_after_signing_and_before_the_broadcast(admin):
    """`on_signed` gets the receipt's hash before the chain has seen it: the intent row's key."""
    wallet, fn, gas, price = _staged(admin)
    seen: list[tuple[str, int, object]] = []

    def on_signed(tx_hash: str) -> None:
        nonce, mined = admin.transaction_count(wallet.address), admin.transaction_receipt(tx_hash)
        seen.append((tx_hash, nonce, mined))

    receipt = admin.send_as_user(wallet, fn, gas=gas, max_fee=price, on_signed=on_signed)

    expected = "0x" + bytes(receipt["transactionHash"]).hex()
    assert seen == [(expected, 0, None)]  # nothing sent, nothing mined, yet


def test_a_hook_that_raises_stops_the_broadcast(admin):
    """An intent row that could not be written must not leave a transaction unrecorded."""
    wallet, fn, gas, price = _staged(admin)

    def on_signed(_tx_hash: str) -> None:
        raise RuntimeError("the database is down")

    with pytest.raises(RuntimeError, match="database is down"):
        admin.send_as_user(wallet, fn, gas=gas, max_fee=price, on_signed=on_signed)
    assert admin.transaction_count(wallet.address) == 0
    assert admin.native_balance(wallet.address) == gas * price


def test_a_receipt_is_read_by_hash_and_an_unknown_hash_reads_none(admin):
    wallet, fn, gas, price = _staged(admin)
    receipt = admin.send_as_user(wallet, fn, gas=gas, max_fee=price)

    found = admin.transaction_receipt("0x" + bytes(receipt["transactionHash"]).hex())

    assert found is not None
    assert found["status"] == 1
    assert found["transactionHash"] == receipt["transactionHash"]
    assert admin.transaction_receipt("0x" + secrets.token_hex(32)) is None
