"""The off-chain ids agree with the CTF actually deployed, for the real oracle
(the admin) and collateral (apUSD) the market path uses."""

import secrets

from eth_utils import keccak
from web3 import Web3

from agentpit.config import Settings
from agentpit.onchain.contracts import Contracts
from agentpit.onchain.ctf_ids import binary_market_ids
from agentpit.onchain.deployment import Deployment


def test_binary_market_ids_match_deployed_ctf():
    settings = Settings()
    d = Deployment.load(settings.deployment_path)
    w3 = Web3(Web3.HTTPProvider(settings.rpc_url_override or d.rpc_url))
    ctf = Contracts(w3, d).ctf
    oracle = Web3.to_checksum_address(d.admin)
    usd = Web3.to_checksum_address(d.usd)
    for _ in range(10):
        qid = keccak(text=f"ctf ids {secrets.token_hex(8)}?")
        cid, tokens = binary_market_ids(oracle, usd, qid)
        assert cid == bytes(ctf.functions.getConditionId(oracle, qid, 2).call())
        for i, token in enumerate(tokens):
            col = ctf.functions.getCollectionId(b"\x00" * 32, cid, 1 << i).call()
            assert token == ctf.functions.getPositionId(usd, col).call()
