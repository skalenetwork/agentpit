"""Off-chain CTF ids must equal what the ConditionalTokens contract returns.

Vectors were read from the CTF deployed on the local anvil with eth_call, so a
wrong port of CTHelpers' curve arithmetic fails here without a chain."""

from eth_utils import keccak
from web3 import Web3

from agentpit.onchain.ctf_ids import (
    binary_market_ids,
    collection_id,
    condition_id,
    position_id,
)

ORACLE = "0x1111111111111111111111111111111111111111"
COLLATERAL = "0x2222222222222222222222222222222222222222"

VECTORS = [
    (
        "Will it rain in Lisbon tomorrow?",
        "0be398dfe4f56dc5874a4b339650a49201e2446351fd0cb82b2fd0ad49e5ca0a",
        (
            "629fbf0bb8d5f982178157805df554ee53f81df578d67dd2752712688b2dba6f",
            81725341691451289196330112371796383432788518278008764180125676975370612866496,
        ),
        (
            "105d85e9231b851b1483f975cfe89d402221bbd856fadf955c2d30561970fb12",
            63400346349793686109371161530033273541034304033765581758419232250591887680961,
        ),
    ),
    (
        "Sync mirror vector?",
        "da0b6ac433353fcc7443b0e27e0ddd78f7d9468c211c99b1c42847beba25467d",
        (
            "62b6b868c7c46b27b256e772784266e1efefa1991f0dfaaafafbd27b03f2a4d2",
            89942875488778846217187920102437452493907598919570984421802875501297451942720,
        ),
        (
            "02c1fde8055926ee36559325ccd1e4a6e5668bd53b7f99c93c8433c64573985f",
            62287030390271063733805781466959194020502084903653473390335682123131320649517,
        ),
    ),
]


def test_ids_match_the_contract():
    for question, cid_hex, (col1, pos1), (col2, pos2) in VECTORS:
        cid = condition_id(ORACLE, keccak(text=question), 2)
        assert cid.hex() == cid_hex
        assert collection_id(cid, 1).hex() == col1
        assert collection_id(cid, 2).hex() == col2
        assert position_id(COLLATERAL, collection_id(cid, 1)) == pos1
        assert position_id(COLLATERAL, collection_id(cid, 2)) == pos2


def test_binary_market_ids_is_index_sets_one_and_two():
    question, cid_hex, (_, pos1), (_, pos2) = VECTORS[0]
    cid, tokens = binary_market_ids(ORACLE, COLLATERAL, keccak(text=question))
    assert cid.hex() == cid_hex
    assert tokens == [pos1, pos2]


def test_lowercase_addresses_are_accepted():
    """Ids do not depend on oracle/collateral address letter case.

    eth_abi's encode_packed treats address strings identically regardless of
    case, so callers may pass lowercase or checksummed addresses and get the
    same ids.
    """
    # Use an address with hex letters
    MIXED = "0xAbCdEF0123456789aBcDeF0123456789AbCdEf01"
    MIXED_COLLATERAL = "0xfEd000000000000000000000000000000000000f"
    qid = keccak(text="test question")

    # Compute with checksum form as reference
    cid_checksum = condition_id(Web3.to_checksum_address(MIXED), qid, 2)

    # Verify lowercase form gives same result
    assert condition_id(MIXED.lower(), qid, 2) == cid_checksum

    # Verify uppercase form gives same result
    assert condition_id(MIXED.upper(), qid, 2) == cid_checksum

    # Test position_id with collateral address in different cases
    col_id = collection_id(cid_checksum, 1)
    pos_checksum = position_id(Web3.to_checksum_address(MIXED_COLLATERAL), col_id)

    # Verify lowercase collateral gives same position_id
    assert position_id(MIXED_COLLATERAL.lower(), col_id) == pos_checksum

    # Verify uppercase collateral gives same position_id
    assert position_id(MIXED_COLLATERAL.upper(), col_id) == pos_checksum
