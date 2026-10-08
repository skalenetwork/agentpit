"""What `UserGasSponsor` is built from: the user-signed call builders on
`OnchainAdmin`, and the reads and send it sizes a top-up with.

The sponsor's promise is "top the wallet up to exactly what the calls need,
then send them", so these pin the facts that promise stands on, against the
real contracts: an empty wallet can be estimated, an exact top-up is enough
and one wei less is not, the gas limit and price the top-up paid for are the
ones sent, a reverted call comes back as a receipt rather than an exception,
and the payout vector reads "not reported" before `reportPayouts` and the
reported numbers after it.
"""

import secrets

import pytest
from eth_account import Account
from web3.exceptions import Web3RPCError

from agentpit.config import Settings
from agentpit.onchain.admin import _MAX_UINT256, OnchainAdmin
from agentpit.onchain.chain_rpc import is_balance_low
from agentpit.onchain.contracts import Contracts
from agentpit.onchain.ctf_ids import binary_market_ids
from agentpit.onchain.deployment import Deployment
from agentpit.onchain.web3_client import Web3Client

HOLDER = "0x9D22FB092E79515611e8380026583929F88815b9"


def _admin() -> OnchainAdmin:
    settings = Settings()
    d = Deployment.load(settings.deployment_path)
    client = Web3Client(settings, d)
    return OnchainAdmin(client, Contracts(client.web3, d))


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


# --- fakes: what must not reach the chain --------------------------------


class _Call:
    def __init__(self, value):
        self._value = value

    def call(self):
        return self._value


class _UnreportedCtf:
    # The ABI's own names, hence the noqa.
    def payoutDenominator(self, _condition_id):  # noqa: N802
        return _Call(0)

    def payoutNumerators(self, _condition_id, _index):  # noqa: N802
        raise AssertionError(
            "an unreported condition's numerators are all zero: reading them "
            "is a round trip for nothing"
        )


class _FakeContracts:
    def __init__(self):
        self.ctf = type("_C", (), {"functions": _UnreportedCtf()})()


class _RecordingFn:
    """A ContractFunction that records the transaction it is estimated with."""

    def __init__(self):
        self.seen: list[dict] = []

    def estimate_gas(self, tx):
        self.seen.append(tx)
        return 46_487


def test_an_unreported_condition_reads_no_numerators():
    admin = OnchainAdmin(client=None, contracts=_FakeContracts())  # type: ignore[arg-type]
    assert admin.payout_vector(b"\x11" * 32) == (0, [0, 0])
    assert admin.payout_vector(b"\x11" * 32, outcome_count=3) == (0, [0, 0, 0])


def test_an_estimate_carries_no_fee_fields():
    """With a gasPrice or maxFeePerGas in it, anvil refuses to estimate for a
    zero-balance sender, and a sponsored wallet is empty when it is sized."""
    admin = OnchainAdmin(client=None, contracts=None)  # type: ignore[arg-type]
    fn = _RecordingFn()
    assert admin.estimate_user_gas(fn, HOLDER.lower()) == 46_487
    assert fn.seen == [{"from": HOLDER}]


# --- reads -----------------------------------------------------------------


def test_payout_vector_before_and_after_report_payouts():
    admin = _admin()
    question_id, condition_id, _tokens = _condition(admin)
    assert admin.payout_vector(condition_id) == (0, [0, 0])

    admin.report_payouts(question_id, [1, 0])
    assert admin.payout_vector(condition_id) == (1, [1, 0])


def test_payout_vector_of_a_no_win_and_of_a_split_resolution():
    admin = _admin()
    no_q, no_cid, _ = _condition(admin)
    half_q, half_cid, _ = _condition(admin)
    admin.report_payouts(no_q, [0, 1])
    admin.report_payouts(half_q, [1, 1])

    assert admin.payout_vector(no_cid) == (1, [0, 1])
    # The denominator is the numerators' sum: each side of a 50/50 pays half.
    assert admin.payout_vector(half_cid) == (2, [1, 1])


def test_gas_price_is_the_nodes_current_price():
    admin = _admin()
    w3 = admin._client.web3  # noqa: SLF001
    assert admin.gas_price() == w3.eth.gas_price


def test_an_empty_wallet_can_be_estimated():
    admin = _admin()
    w3 = admin._client.web3  # noqa: SLF001
    wallet = Account.create()
    assert w3.eth.get_balance(wallet.address) == 0

    # Measured on anvil 2026-10-08: 46,487 and 45,996.
    for fn in admin.approval_calls():
        assert 21_000 < admin.estimate_user_gas(fn, wallet.address) < 100_000


def test_a_claim_with_nothing_to_claim_still_estimates():
    """redeemPositions succeeds with zero holdings, so the chain never refuses
    a worthless claim and the sponsor has to gate it before paying for it."""
    admin = _admin()
    question_id, condition_id, _tokens = _condition(admin)
    admin.report_payouts(question_id, [1, 0])

    nobody = Account.create().address
    claim = admin.redeem_call(condition_id, [1, 2])
    assert admin.estimate_user_gas(claim, nobody) > 21_000


def test_transaction_count_counts_only_what_the_wallet_sent():
    admin = _admin()
    wallet = Account.create()
    assert admin.transaction_count(wallet.address) == 0

    fn = admin.approval_calls()[0]
    gas = _sized(admin, fn, wallet.address)
    price = admin.gas_price()
    admin.fund_gas(wallet.address, gas * price)
    # A top-up is a transaction *to* the wallet: its own count stays 0, which
    # is what tells a wiped chain from a wallet spent down to nothing.
    assert admin.transaction_count(wallet.address) == 0

    admin.send_as_user(wallet, fn, gas=gas, max_fee=price)
    assert admin.transaction_count(wallet.address) == 1
    assert admin.transaction_count(wallet.address.lower()) == 1


# --- sending as the user ---------------------------------------------------


def test_send_as_user_sends_exactly_the_gas_and_price_it_is_given():
    admin = _admin()
    w3 = admin._client.web3  # noqa: SLF001
    wallet = Account.create()
    fn = admin.approval_calls()[0]
    # Deliberately not estimate × 1.2: had send_user_tx estimated again, the
    # limit on the mined transaction would not be this number.
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


def test_one_wei_short_of_gas_times_price_is_a_balance_refusal():
    admin = _admin()
    wallet = Account.create()
    fn = admin.approval_calls()[0]
    gas = _sized(admin, fn, wallet.address)
    price = admin.gas_price()
    admin.fund_gas(wallet.address, gas * price - 1)

    with pytest.raises(Web3RPCError) as caught:
        admin.send_as_user(wallet, fn, gas=gas, max_fee=price)
    # anvil's wording ("Insufficient funds for gas * price + value"); the
    # sponsor resizes and retries once on exactly this.
    assert is_balance_low(caught.value)
    assert admin.transaction_count(wallet.address) == 0


def test_a_reverted_call_is_mined_and_returned():
    """With the limit given nothing is estimated, so a call the contract
    rejects is mined, paid for, and handed back with status 0. The sponsor
    books its gas and raises; it never sees an exception here."""
    admin = _admin()
    _question_id, condition_id, _tokens = _condition(admin)  # not reported
    wallet = Account.create()
    gas, price = 100_000, admin.gas_price()
    admin.fund_gas(wallet.address, gas * price)

    receipt = admin.send_as_user(
        wallet, admin.redeem_call(condition_id, [1, 2]), gas=gas, max_fee=price
    )

    assert receipt["status"] == 0
    assert receipt["gasUsed"] > 0
    assert admin.transaction_count(wallet.address) == 1


# --- the call builders ------------------------------------------------------


def test_approval_calls_are_the_three_onboarding_approvals_in_order():
    admin = _admin()
    contracts = admin._contracts  # noqa: SLF001
    exchange = contracts.exchange.address
    calls = admin.approval_calls()

    assert [(fn.address, fn.fn_name, fn.args) for fn in calls] == [
        (contracts.usd.address, "approve", (exchange, _MAX_UINT256)),
        (contracts.usd.address, "approve", (contracts.ctf.address, _MAX_UINT256)),
        (contracts.ctf.address, "setApprovalForAll", (exchange, True)),
    ]


def test_one_exact_top_up_pays_for_all_three_approvals():
    """Onboarding without a grant: one top-up sized for the three calls
    together, then the three sent back to back from it."""
    admin = _admin()
    contracts = admin._contracts  # noqa: SLF001
    wallet = Account.create()
    calls = admin.approval_calls()
    limits = [_sized(admin, fn, wallet.address) for fn in calls]
    price = admin.gas_price()
    need = sum(limits) * price
    admin.fund_gas(wallet.address, need)

    receipts = [
        admin.send_as_user(wallet, fn, gas=gas, max_fee=price)
        for fn, gas in zip(calls, limits)
    ]

    assert [r["status"] for r in receipts] == [1, 1, 1]
    exchange = contracts.exchange.address
    usd = contracts.usd.functions
    assert usd.allowance(wallet.address, exchange).call() == _MAX_UINT256
    assert usd.allowance(wallet.address, contracts.ctf.address).call() == _MAX_UINT256
    assert contracts.ctf.functions.isApprovedForAll(wallet.address, exchange).call()
    assert admin.native_balance(wallet.address) <= need


def test_grant_user_approvals_still_sets_every_approval():
    """The house path: a wallet that holds its own gas signs all three."""
    admin = _admin()
    contracts = admin._contracts  # noqa: SLF001
    house = Account.create()
    admin.fund_gas(house.address, 10**16)

    receipts = admin.grant_user_approvals(house)

    assert [r["status"] for r in receipts] == [1, 1, 1]
    exchange = contracts.exchange.address
    usd = contracts.usd.functions
    assert usd.allowance(house.address, exchange).call() == _MAX_UINT256
    assert usd.allowance(house.address, contracts.ctf.address).call() == _MAX_UINT256
    assert contracts.ctf.functions.isApprovedForAll(house.address, exchange).call()


def test_split_merge_and_redeem_calls_move_collateral_and_tokens():
    admin = _admin()
    question_id, condition_id, tokens = _condition(admin)
    wallet = Account.create()
    admin.fund_gas(wallet.address, 10**16)
    admin.faucet_drip(wallet.address)
    admin.grant_user_approvals(wallet)
    usd_start = admin.usd_balance(wallet.address)

    def send(fn):
        gas = _sized(admin, fn, wallet.address)
        receipt = admin.send_as_user(wallet, fn, gas=gas, max_fee=admin.gas_price())
        assert receipt["status"] == 1
        return receipt

    split = admin.split_call(condition_id, [1, 2], 5_000_000)
    assert split.args == (
        admin.collateral_address,
        b"\x00" * 32,
        condition_id,
        [1, 2],
        5_000_000,
    )
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


def test_user_split_position_still_splits_for_a_self_funded_wallet():
    admin = _admin()
    _question_id, condition_id, tokens = _condition(admin)
    house = Account.create()
    admin.fund_gas(house.address, 10**16)
    admin.faucet_drip(house.address)
    admin.grant_user_approvals(house)

    receipt = admin.user_split_position(house, condition_id, 7_000_000)

    assert receipt["status"] == 1
    assert admin.ctf_balances(house.address, tokens) == [7_000_000, 7_000_000]


# --- the signing hook and the receipt read -----------------------------------


def test_the_hash_is_handed_over_after_signing_and_before_the_broadcast():
    """`on_signed` gets the transaction's own hash, the one its receipt will
    carry, while the chain has not seen it yet: what `PositionService` writes
    its intent row under before anything can mine."""
    admin = _admin()
    wallet = Account.create()
    fn = admin.approval_calls()[0]
    gas, price = _sized(admin, fn, wallet.address), admin.gas_price()
    admin.fund_gas(wallet.address, gas * price)
    seen: list[tuple[str, int, object]] = []

    def on_signed(tx_hash: str) -> None:
        seen.append(
            (
                tx_hash,
                admin.transaction_count(wallet.address),
                admin.transaction_receipt(tx_hash),
            )
        )

    receipt = admin.send_as_user(wallet, fn, gas=gas, max_fee=price, on_signed=on_signed)

    expected = "0x" + bytes(receipt["transactionHash"]).hex()
    assert seen == [(expected, 0, None)]  # nothing sent, nothing mined, yet


def test_a_hook_that_raises_stops_the_broadcast():
    """An intent row that could not be written must not leave a transaction
    on its way that nothing records."""
    admin = _admin()
    wallet = Account.create()
    fn = admin.approval_calls()[0]
    gas, price = _sized(admin, fn, wallet.address), admin.gas_price()
    admin.fund_gas(wallet.address, gas * price)

    def on_signed(_tx_hash: str) -> None:
        raise RuntimeError("the database is down")

    with pytest.raises(RuntimeError, match="database is down"):
        admin.send_as_user(wallet, fn, gas=gas, max_fee=price, on_signed=on_signed)
    assert admin.transaction_count(wallet.address) == 0
    assert admin.native_balance(wallet.address) == gas * price


def test_a_receipt_is_read_by_hash_and_an_unknown_hash_reads_none():
    admin = _admin()
    wallet = Account.create()
    fn = admin.approval_calls()[0]
    gas, price = _sized(admin, fn, wallet.address), admin.gas_price()
    admin.fund_gas(wallet.address, gas * price)
    receipt = admin.send_as_user(wallet, fn, gas=gas, max_fee=price)
    tx_hash = "0x" + bytes(receipt["transactionHash"]).hex()

    found = admin.transaction_receipt(tx_hash)

    assert found is not None
    assert found["status"] == 1
    assert found["transactionHash"] == receipt["transactionHash"]
    assert admin.transaction_receipt("0x" + secrets.token_hex(32)) is None
