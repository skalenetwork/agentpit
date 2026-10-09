"""Helpers for sending transactions signed by a user's private key."""

from collections.abc import Callable

from eth_account.signers.local import LocalAccount
from web3.contract.contract import ContractFunction
from web3.types import TxReceipt

from agentpit.onchain.chain_rpc import (
    ReceiptUnreachable,
    current_fee_params,
    failed_before_connecting,
)
from agentpit.onchain.web3_client import Web3Client

# The pad on a user transaction's gas estimate, also what `UserGasSponsor` and
# the house's approvals are sized with.
GAS_BUFFER_PCT = 20


def send_user_tx(
    client: Web3Client,
    user_account: LocalAccount,
    fn: ContractFunction,
    *,
    timeout: int = 30,
    gas_buffer_pct: int = GAS_BUFFER_PCT,
    gas: int | None = None,
    max_fee: int | None = None,
    on_signed: Callable[[str], None] | None = None,
) -> TxReceipt:
    """Build, sign and broadcast `fn(...)` from the user's account; wait for receipt.

    Each user has their own nonce stream, so unlike the admin key's it needs no
    shared sender.

    `gas` and `max_fee` are for a caller that has sized the transaction and
    funded exactly `gas x max_fee` (`UserGasSponsor`): used as given, with no
    tip and no second estimate (a ~0.5 s RPC on SKALE whose answer could
    disagree with what was funded). Either one left out is worked out here.

    `on_signed` gets the transaction's hash ("0x" and lowercase hex) once it
    is signed and before the broadcast, so a caller can record it before it
    can mine. If the hook raises, nothing is broadcast.

    An error that passes `failed_before_connecting` means the transaction
    never reached the node. A receipt poll that cannot connect is raised as
    `ReceiptUnreachable` instead: the node has the transaction, and it may
    mine.
    """
    web3 = client.web3
    nonce = web3.eth.get_transaction_count(user_account.address, "pending")
    if gas is None:
        # Estimated before any price is set: with one, a dry account's
        # estimate already fails, before the send can say it is short of gas
        # (what `InsufficientGasError` is made from).
        estimate = fn.estimate_gas({"from": user_account.address})
        gas = estimate * (100 + gas_buffer_pct) // 100
    if max_fee is None:
        # Not web3's default fee: skaled bills maxFeePerGas in full.
        max_fee, priority = current_fee_params(web3)
    else:
        priority = 0  # the same no-tip rule `current_fee_params` applies
    # Every field filled in, so web3 builds this without a single RPC.
    tx = fn.build_transaction(
        {
            "from": user_account.address,
            "nonce": nonce,
            "chainId": client.deployment.chain_id,
            "gas": gas,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": priority,
        }
    )
    signed = user_account.sign_transaction(tx)
    if on_signed is not None:
        on_signed("0x" + bytes(signed.hash).hex())
    tx_hash = web3.eth.send_raw_transaction(signed.raw_transaction)
    try:
        return web3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
    except Exception as exc:
        if failed_before_connecting(exc):
            raise ReceiptUnreachable(
                f"the node took the transaction but its receipt poll could not "
                f"connect: {exc}"
            ) from exc
        raise


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
    client: Web3Client,
    user_address: str,
    value_wei: int,
    *,
    timeout: int = 30,
    slot_timeout: float | None = None,
) -> TxReceipt:
    """Send `value_wei` native tokens from admin to user_address, through
    `OnchainAdmin.fund_gas`: `UserGasSponsor`'s top-ups and the house gas
    loop. `slot_timeout` is `AdminTxSender.send_value`'s."""
    return client.admin_sender.send_value(
        user_address, value_wei, timeout=timeout, slot_timeout=slot_timeout
    )
