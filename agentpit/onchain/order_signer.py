"""EIP-712 signing of CTFExchange Order structs."""

from dataclasses import asdict, dataclass

from eth_account.messages import encode_typed_data
from eth_account.signers.local import LocalAccount
from web3 import Web3

from agentpit.onchain.deployment import Deployment


@dataclass(frozen=True)
class OrderData:
    """Mirrors the Solidity Order struct in OrderStructs.sol."""

    salt: int
    maker: str
    signer: str
    taker: str
    tokenId: int
    makerAmount: int
    takerAmount: int
    expiration: int
    nonce: int
    feeRateBps: int
    side: int  # 0 = BUY, 1 = SELL
    signatureType: int  # 0 = EOA / EIP712


_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "Order": [
        {"name": "salt", "type": "uint256"},
        {"name": "maker", "type": "address"},
        {"name": "signer", "type": "address"},
        {"name": "taker", "type": "address"},
        {"name": "tokenId", "type": "uint256"},
        {"name": "makerAmount", "type": "uint256"},
        {"name": "takerAmount", "type": "uint256"},
        {"name": "expiration", "type": "uint256"},
        {"name": "nonce", "type": "uint256"},
        {"name": "feeRateBps", "type": "uint256"},
        {"name": "side", "type": "uint8"},
        {"name": "signatureType", "type": "uint8"},
    ],
}


def _domain(deployment: Deployment) -> dict:
    return {
        "name": "Polymarket CTF Exchange",
        "version": "1",
        "chainId": deployment.chain_id,
        "verifyingContract": Web3.to_checksum_address(deployment.exchange),
    }


def sign_order(
    user_account: LocalAccount, deployment: Deployment, order: OrderData
) -> bytes:
    message = asdict(order)
    for key in ("maker", "signer", "taker"):
        message[key] = Web3.to_checksum_address(message[key])

    encoded = encode_typed_data(
        domain_data=_domain(deployment),
        message_types={"Order": _TYPES["Order"]},
        message_data=message,
    )
    return bytes(user_account.sign_message(encoded).signature)
