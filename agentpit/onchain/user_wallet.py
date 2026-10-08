"""Helpers for sending transactions signed by a user's private key."""

from eth_account.signers.local import LocalAccount
from web3.contract.contract import ContractFunction
from web3.types import TxReceipt

from agentpit.onchain.chain_rpc import current_fee_params
from agentpit.onchain.web3_client import Web3Client


def send_user_tx(
    client: Web3Client,
    user_account: LocalAccount,
    fn: ContractFunction,
    *,
    timeout: int = 30,
    gas_buffer_pct: int = 20,
) -> TxReceipt:
    """Build, sign and broadcast `fn(...)` from the user's account; wait for receipt.

    Each user has their own nonce stream, so unlike the admin key's it needs no
    shared sender.
    """
    web3 = client.web3
    nonce = web3.eth.get_transaction_count(user_account.address, "pending")
    # Estimated before any price is set: with one, a dry account's estimate
    # already fails, before the send can say it is short of gas (what
    # `InsufficientGasError` is made from).
    gas_estimate = fn.estimate_gas({"from": user_account.address})
    # Not web3's default fee: skaled bills maxFeePerGas in full.
    max_fee, priority = current_fee_params(web3)
    # Every field filled in, so web3 builds this without a single RPC.
    tx = fn.build_transaction(
        {
            "from": user_account.address,
            "nonce": nonce,
            "chainId": client.deployment.chain_id,
            "gas": gas_estimate * (100 + gas_buffer_pct) // 100,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": priority,
        }
    )
    signed = user_account.sign_transaction(tx)
    tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
    return web3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)


def send_admin_tx(
    client: Web3Client,
    fn: ContractFunction,
    *,
    timeout: int = 30,
    gas_buffer_pct: int = 20,
    essential: bool = False,
) -> TxReceipt:
    """Build, sign and broadcast `fn(...)` from the admin account; wait for it.

    Goes through `client.admin_sender`, so concurrent admin sends no longer
    wait for each other's receipts: they get consecutive nonces and share
    blocks. Returns the receipt whatever its status.

    Sponsored unless `essential`: see `AdminTxSender._gate`.
    """
    return client.admin_sender.send(
        fn, timeout=timeout, gas_buffer_pct=gas_buffer_pct, essential=essential
    )


def fund_user_with_native(
    client: Web3Client, user_address: str, value_wei: int, *, timeout: int = 30
) -> TxReceipt:
    """Send `value_wei` native tokens from admin to user_address.

    Used at signup so the new user can pay gas for their three approval txns.
    """
    return client.admin_sender.send_value(user_address, value_wei, timeout=timeout)
