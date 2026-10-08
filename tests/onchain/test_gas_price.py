"""Every transaction we send is priced at the node's current `eth_gasPrice`
with no tip. skaled bills `maxFeePerGas` in full (`effectiveGasPrice =
maxFeePerGas`), so any headroom above the current price is paid for nothing:
web3's default of twice the base fee plus a tip doubled every on-chain cost."""

import secrets

import pytest
from eth_account import Account
from web3.exceptions import Web3RPCError

from agentpit.config import Settings
from agentpit.onchain.admin import OnchainAdmin
from agentpit.onchain.chain_rpc import (
    SendError,
    Web3ChainRpc,
    classify_send_error,
    is_balance_low,
)
from agentpit.onchain.contracts import Contracts
from agentpit.onchain.deployment import Deployment
from agentpit.onchain.tx_sender import PendingTx
from agentpit.onchain.user_wallet import send_user_tx
from agentpit.onchain.web3_client import Web3Client


def _admin() -> OnchainAdmin:
    # A fresh client, so its admin sender has no fee cached yet.
    settings = Settings()
    d = Deployment.load(settings.deployment_path)
    client = Web3Client(settings, d)
    return OnchainAdmin(client, Contracts(client.web3, d))


def _assert_priced_at(w3, tx_hash, price: int) -> None:
    tx = w3.eth.get_transaction(tx_hash)
    assert tx["type"] == 2
    assert tx["maxFeePerGas"] == price
    assert tx["maxPriorityFeePerGas"] == 0


def test_fee_params_are_the_current_gas_price_and_no_tip():
    w3 = _admin()._client.web3  # noqa: SLF001
    assert Web3ChainRpc(w3).fee_params() == (w3.eth.gas_price, 0)


def test_a_blocking_admin_send_pays_the_current_gas_price():
    admin = _admin()
    w3 = admin._client.web3  # noqa: SLF001
    price = w3.eth.gas_price
    receipt = admin.faucet_drip(Account.create().address)
    assert receipt["status"] == 1
    _assert_priced_at(w3, receipt["transactionHash"], price)


def test_a_gas_grant_pays_the_current_gas_price():
    admin = _admin()
    w3 = admin._client.web3  # noqa: SLF001
    price = w3.eth.gas_price
    receipt = admin.fund_gas(Account.create().address, 10**15)
    assert receipt["status"] == 1
    _assert_priced_at(w3, receipt["transactionHash"], price)


def test_a_batch_pays_the_current_gas_price():
    admin = _admin()
    w3 = admin._client.web3  # noqa: SLF001
    price = w3.eth.gas_price
    calls = [
        admin.prepare_condition_call(admin.oracle_address, secrets.token_bytes(32), 2)
        for _ in range(3)
    ]
    pendings = [p for p in admin.submit_many(calls) if isinstance(p, PendingTx)]
    assert len(pendings) == 3
    receipts = admin.wait_all(pendings, timeout=30)
    assert all(not isinstance(r, Exception) and r["status"] == 1 for r in receipts)
    for p in pendings:
        _assert_priced_at(w3, p.tx_hash, price)


def test_a_user_tx_pays_the_current_gas_price():
    admin = _admin()
    client = admin._client  # noqa: SLF001
    w3 = client.web3
    user = Account.create()
    admin.fund_gas(user.address, 10**16)
    usd = admin._contracts.usd  # noqa: SLF001
    price = w3.eth.gas_price
    receipt = send_user_tx(client, user, usd.functions.approve(user.address, 1))
    assert receipt["status"] == 1
    _assert_priced_at(w3, receipt["transactionHash"], price)


def _refuse_estimate(*_args, **_kwargs):
    raise AssertionError("send_user_tx estimated gas although a limit was given")


def test_a_user_tx_with_a_given_gas_limit_sends_it_without_estimating(monkeypatch):
    """`UserGasSponsor` funds the wallet for exactly the limit it estimated,
    and passes that limit in: a second estimate would cost another RPC (~0.5 s
    on SKALE) and could disagree with what was funded."""
    admin = _admin()
    client = admin._client  # noqa: SLF001
    w3 = client.web3
    user = Account.create()
    admin.fund_gas(user.address, 10**16)
    fn = admin._contracts.usd.functions.approve(user.address, 1)  # noqa: SLF001
    monkeypatch.setattr(fn, "estimate_gas", _refuse_estimate)
    price = w3.eth.gas_price
    receipt = send_user_tx(client, user, fn, gas=80_000)
    assert receipt["status"] == 1
    assert w3.eth.get_transaction(receipt["transactionHash"])["gas"] == 80_000
    _assert_priced_at(w3, receipt["transactionHash"], price)


def test_a_user_tx_with_a_given_fee_pays_that_fee_and_no_tip():
    admin = _admin()
    client = admin._client  # noqa: SLF001
    w3 = client.web3
    user = Account.create()
    admin.fund_gas(user.address, 10**16)
    fee = w3.eth.gas_price + 12_345  # not what the node would pick by itself
    fn = admin._contracts.usd.functions.approve(user.address, 1)  # noqa: SLF001
    receipt = send_user_tx(client, user, fn, max_fee=fee)
    assert receipt["status"] == 1
    _assert_priced_at(w3, receipt["transactionHash"], fee)


def test_a_dry_wallet_send_reads_as_balance_low_on_anvil(monkeypatch):
    """anvil's own refusal for a sender that cannot pay gas x maxFeePerGas,
    read off the real node: what `UserGasSponsor` resizes and retries on.
    It stays `SendError.OTHER`, so `AdminTxSender` behaves as before."""
    admin = _admin()
    client = admin._client  # noqa: SLF001
    user = Account.create()  # never funded
    fn = admin._contracts.usd.functions.approve(user.address, 1)  # noqa: SLF001
    monkeypatch.setattr(fn, "estimate_gas", _refuse_estimate)
    with pytest.raises(Web3RPCError) as info:
        send_user_tx(
            client, user, fn, gas=60_000, max_fee=client.web3.eth.gas_price
        )
    assert is_balance_low(info.value)
    assert classify_send_error(info.value) is SendError.OTHER
