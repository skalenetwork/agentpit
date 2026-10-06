"""Conditional Tokens ids, computed locally instead of asked of the chain.

Each of these used to be an eth_call per market, and an eth_call costs
0.2-0.5 s against SKALE. They are pure functions of their inputs, ported from
Gnosis CTHelpers (the library Polymarket's ConditionalTokens deploys):

- getConditionId = keccak256(abi.encodePacked(oracle, questionId, slotCount))
- getCollectionId(parent = 0, conditionId, indexSet): hash onto alt_bn128 and
  compress the point. A non-zero parent adds a second point (ecAdd); agentpit
  never nests positions, so only the zero-parent form exists here.
- getPositionId = uint256(keccak256(abi.encodePacked(collateral, collectionId)))
"""

from eth_abi.packed import encode_packed
from eth_utils import keccak
from web3 import Web3

# alt_bn128 base field modulus, and B in y^2 = x^3 + B.
_P = 0x30644E72E131A029B85045B68181585D97816A916871CA8D3C208C16D87CFD47
_B = 3


def condition_id(oracle: str, question_id: bytes, outcome_slot_count: int) -> bytes:
    return keccak(
        encode_packed(
            ["address", "bytes32", "uint256"],
            [Web3.to_checksum_address(oracle), question_id, outcome_slot_count],
        )
    )


def collection_id(condition_id_: bytes, index_set: int) -> bytes:
    """CTHelpers.getCollectionId with parentCollectionId = 0."""
    x = int.from_bytes(
        keccak(encode_packed(["bytes32", "uint256"], [condition_id_, index_set])),
        "big",
    )
    odd = (x >> 255) != 0
    while True:
        x = (x + 1) % _P
        yy = (x * x % _P * x + _B) % _P
        # _P % 4 == 3, so this is the square root whenever one exists.
        y = pow(yy, (_P + 1) // 4, _P)
        if y * y % _P == yy:
            break
    if (odd and y % 2 == 0) or (not odd and y % 2 == 1):
        y = _P - y
    if y % 2 == 1:
        x ^= 1 << 254
    return x.to_bytes(32, "big")


def position_id(collateral: str, collection_id_: bytes) -> int:
    return int.from_bytes(
        keccak(
            encode_packed(
                ["address", "bytes32"],
                [Web3.to_checksum_address(collateral), collection_id_],
            )
        ),
        "big",
    )


def binary_market_ids(
    oracle: str, collateral: str, question_id: bytes
) -> tuple[bytes, list[int]]:
    """(conditionId, [token of index set 1, token of index set 2]) for a
    two-outcome market — the YES/NO order every agentpit market uses."""
    cid = condition_id(oracle, question_id, 2)
    return cid, [position_id(collateral, collection_id(cid, 1 << i)) for i in range(2)]
