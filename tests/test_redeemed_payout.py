"""`OnchainAdmin.redeemed_payout`: what a claim paid, read from its receipt.

A claim is logged at the amount the CTF's `PayoutRedemption` event reports,
not at the change in the wallet's apUSD: fills, mints and transfers move that
balance while the claim is in flight. So the event is the figure, and only the
CTF's own events for the redeemer count. A receipt can carry other contracts'
logs (a token's `Transfer`, a lookalike contract emitting the same event) and
other accounts' redemptions (a batched call), and none of those is the user's.

The receipts here are built by hand and the admin has no node behind it:
decoding a receipt reads nothing from the chain. tests/onchain/
test_sponsored_positions.py compares the figure with a real claim on anvil.
"""

from __future__ import annotations

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


def _admin() -> OnchainAdmin:
    zero = "0x" + "00" * 20
    deployment = Deployment(
        chain_id=31337,
        rpc_url="http://127.0.0.1:1",
        admin=zero,
        usd=_USD,
        faucet=zero,
        ctf=_CTF,
        proxy_factory=zero,
        safe_factory=zero,
        exchange=zero,
        signup_grant_raw=100_000_000,
    )
    # No provider, no client: decoding a receipt makes no call.
    return OnchainAdmin(None, Contracts(Web3(), deployment))  # type: ignore[arg-type]


def _word(address: str) -> bytes:
    return bytes(12) + bytes.fromhex(address[2:])


def _log(index: int, topics: list[bytes], data: bytes, *, address: str = _CTF) -> dict:
    return {
        "address": address,
        "topics": [HexBytes(t) for t in topics],
        "data": HexBytes(data),
        "blockNumber": 7,
        "blockHash": HexBytes(b"\x01" * 32),
        "transactionHash": HexBytes(b"\x02" * 32),
        "transactionIndex": 0,
        "logIndex": index,
        "removed": False,
    }


def _redemption(index: int, redeemer: str, payout: int, *, address: str = _CTF) -> dict:
    """A `PayoutRedemption(redeemer, collateral, parent, condition, [1, 2],
    payout)` log: the first three arguments are topics, the rest data."""
    return _log(
        index,
        [_PAYOUT_REDEMPTION, _word(redeemer), _word(_USD), bytes(32)],
        encode(["bytes32", "uint256[]", "uint256"], [b"\xcd" * 32, [1, 2], payout]),
        address=address,
    )


def _split(index: int, stakeholder: str, amount: int) -> dict:
    return _log(
        index,
        [_POSITION_SPLIT, _word(stakeholder), bytes(32), b"\xcd" * 32],
        encode(["address", "uint256[]", "uint256"], [_USD, [1, 2], amount]),
    )


def _transfer(index: int, to: str, amount: int) -> dict:
    """An ERC-20 `Transfer`: the apUSD paid out by the claim, a different
    event on a different contract."""
    return _log(
        index,
        [_TRANSFER, _word(_CTF), _word(to)],
        encode(["uint256"], [amount]),
        address=_USD,
    )


def _receipt(*logs: dict) -> dict:
    return {"status": 1, "logs": list(logs)}


def test_a_receipt_with_the_redeemers_payout_reads_it():
    receipt = _receipt(
        _transfer(0, _USER, 100_000_000), _redemption(1, _USER, 100_000_000)
    )

    assert _admin().redeemed_payout(receipt, _USER) == 100_000_000


def test_the_redeemer_is_matched_whatever_the_case():
    receipt = _receipt(_redemption(0, _USER, 42_000_000))
    admin = _admin()

    assert admin.redeemed_payout(receipt, _USER.lower()) == 42_000_000
    assert admin.redeemed_payout(receipt, "0x" + _USER[2:].upper()) == 42_000_000


def test_another_redeemers_payout_is_not_the_users():
    receipt = _receipt(
        _redemption(0, _OTHER, 70_000_000), _redemption(1, _USER, 30_000_000)
    )

    assert _admin().redeemed_payout(receipt, _USER) == 30_000_000
    assert _admin().redeemed_payout(receipt, _OTHER) == 70_000_000


def test_a_payout_logged_by_another_contract_is_ignored():
    """The same event, with the same signature, emitted by an address that is
    not the CTF: not a redemption."""
    receipt = _receipt(_redemption(0, _USER, 500_000_000, address=_LOOKALIKE))

    assert _admin().redeemed_payout(receipt, _USER) == 0


def test_a_lookalike_does_not_add_to_the_real_payout():
    receipt = _receipt(
        _redemption(0, _USER, 500_000_000, address=_LOOKALIKE),
        _redemption(1, _USER, 100_000_000),
    )

    assert _admin().redeemed_payout(receipt, _USER) == 100_000_000


def test_the_ctfs_other_events_are_not_payouts():
    receipt = _receipt(_split(0, _USER, 100_000_000))

    assert _admin().redeemed_payout(receipt, _USER) == 0


def test_a_receipt_without_logs_pays_nothing():
    assert _admin().redeemed_payout(_receipt(), _USER) == 0


def test_a_log_that_does_not_decode_is_skipped_not_raised():
    """The right signature, the wrong data. The CTF never emits it, but a claim
    that mined must not turn into an exception over a log it could not read."""
    broken = _log(
        0, [_PAYOUT_REDEMPTION, _word(_USER), _word(_USD), bytes(32)], b"\x01\x02"
    )
    receipt = _receipt(broken, _redemption(1, _USER, 9_000_000))

    assert _admin().redeemed_payout(receipt, _USER) == 9_000_000


def test_several_redemptions_for_the_redeemer_add_up():
    receipt = _receipt(
        _redemption(0, _USER, 60_000_000),
        _redemption(1, _OTHER, 5_000_000),
        _redemption(2, _USER, 40_000_000),
    )

    assert _admin().redeemed_payout(receipt, _USER) == 100_000_000
