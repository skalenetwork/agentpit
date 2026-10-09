"""`OnchainAdmin.redeemed_payout`: what a claim paid, read from its receipt.

A claim is logged at the amount the CTF's `PayoutRedemption` event reports, not at the change
in the wallet's apUSD (fills, mints and transfers move that while the claim is in flight).
Only the CTF's own events for the redeemer count: a receipt can carry other contracts' logs
and other accounts' redemptions (a batched call). Receipts are built by hand and the admin has
no node behind it; tests/onchain/test_sponsored_positions.py compares with a real claim.
"""

from __future__ import annotations

import pytest
from eth_abi import encode
from hexbytes import HexBytes
from web3 import Web3

from agentpit.onchain.admin import OnchainAdmin
from agentpit.onchain.contracts import Contracts
from agentpit.onchain.deployment import Deployment

_CTF = Web3.to_checksum_address("0x" + "c7" * 20)
_USD = Web3.to_checksum_address("0x" + "a5" * 20)
_USER = Web3.to_checksum_address("0x" + "11" * 20)
_OTHER = Web3.to_checksum_address("0x" + "22" * 20)
_LOOKALIKE = Web3.to_checksum_address("0x" + "99" * 20)
_PAYOUT_REDEMPTION = Web3.keccak(
    text="PayoutRedemption(address,address,bytes32,bytes32,uint256[],uint256)"
)
_POSITION_SPLIT = Web3.keccak(
    text="PositionSplit(address,address,bytes32,bytes32,uint256[],uint256)"
)
_TRANSFER = Web3.keccak(text="Transfer(address,address,uint256)")
_HUNDRED = 100_000_000


def _admin() -> OnchainAdmin:
    zero = "0x" + "00" * 20
    deployment = Deployment(
        chain_id=31337, rpc_url="http://127.0.0.1:1", admin=zero, usd=_USD, faucet=zero,
        ctf=_CTF, proxy_factory=zero, safe_factory=zero, exchange=zero, signup_grant_raw=_HUNDRED,
    )
    # No provider, no client: decoding a receipt makes no call.
    return OnchainAdmin(None, Contracts(Web3(), deployment))  # type: ignore[arg-type]


def _word(address: str) -> bytes:
    return bytes(12) + bytes.fromhex(address[2:])


def _log(index: int, topics: list[bytes], data: bytes, *, address: str = _CTF) -> dict:
    return {
        "address": address, "topics": [HexBytes(t) for t in topics], "data": HexBytes(data),
        "blockNumber": 7, "blockHash": HexBytes(b"\x01" * 32),
        "transactionHash": HexBytes(b"\x02" * 32), "transactionIndex": 0, "logIndex": index,
        "removed": False,
    }


def _redemption(index: int, redeemer: str, payout: int, *, address: str = _CTF) -> dict:
    # `PayoutRedemption(redeemer, collateral, parent, condition, [1, 2], payout)`:
    # the first three arguments are topics, the rest data.
    topics = [_PAYOUT_REDEMPTION, _word(redeemer), _word(_USD), bytes(32)]
    data = encode(["bytes32", "uint256[]", "uint256"], [b"\xcd" * 32, [1, 2], payout])
    return _log(index, topics, data, address=address)


def _split(index: int, stakeholder: str, amount: int) -> dict:
    topics = [_POSITION_SPLIT, _word(stakeholder), bytes(32), b"\xcd" * 32]
    return _log(index, topics, encode(["address", "uint256[]", "uint256"], [_USD, [1, 2], amount]))


def _transfer(index: int, to: str, amount: int) -> dict:
    # An ERC-20 `Transfer`: the apUSD the claim paid out, another event on another contract.
    topics = [_TRANSFER, _word(_CTF), _word(to)]
    return _log(index, topics, encode(["uint256"], [amount]), address=_USD)


_BROKEN = _log(0, [_PAYOUT_REDEMPTION, _word(_USER), _word(_USD), bytes(32)], b"\x01\x02")
_BATCH = [_redemption(0, _OTHER, 70_000_000), _redemption(1, _USER, 30_000_000)]
# The same event, not from the CTF.
_LOOKALIKE_PAYOUT = _redemption(0, _USER, 500_000_000, address=_LOOKALIKE)


@pytest.mark.parametrize(
    ("logs", "redeemer", "paid"),
    [
        pytest.param(
            [_transfer(0, _USER, _HUNDRED), _redemption(1, _USER, _HUNDRED)], _USER, _HUNDRED,
            id="the-redeemers-payout",
        ),
        pytest.param([_redemption(0, _USER, 42_000_000)], _USER.lower(), 42_000_000, id="lower"),
        pytest.param(
            [_redemption(0, _USER, 42_000_000)], "0x" + _USER[2:].upper(), 42_000_000, id="upper"
        ),
        pytest.param(_BATCH, _USER, 30_000_000, id="another-redeemers-payout-is-not-the-users"),
        pytest.param(_BATCH, _OTHER, 70_000_000, id="and-the-users-is-not-the-others"),
        pytest.param([_LOOKALIKE_PAYOUT], _USER, 0, id="other-contract"),
        pytest.param(
            [_LOOKALIKE_PAYOUT, _redemption(1, _USER, _HUNDRED)], _USER, _HUNDRED,
            id="lookalike-adds-nothing",
        ),
        pytest.param([_split(0, _USER, _HUNDRED)], _USER, 0, id="other-ctf-events"),
        pytest.param([], _USER, 0, id="no-logs"),
        # Right signature, wrong data: a mined claim must not raise over a log it cannot read.
        pytest.param(
            [_BROKEN, _redemption(1, _USER, 9_000_000)], _USER, 9_000_000, id="undecodable"
        ),
        pytest.param(
            [_redemption(0, _USER, 60_000_000), _redemption(1, _OTHER, 5_000_000),
             _redemption(2, _USER, 40_000_000)],
            _USER, _HUNDRED, id="several-add-up",
        ),
    ],
)
def test_redeemed_payout_reads_only_the_redeemers_ctf_payouts(logs, redeemer, paid):
    assert _admin().redeemed_payout({"status": 1, "logs": logs}, redeemer) == paid
