"""Check, against a live chain, that one key lands several transactions per block.

    MTM_SMOKE_PK=0x... RPC_URL=https://... python scripts/mtm_smoke.py [count]

Sends `count` (default 10) 0-value transfers to itself through AdminTxSender
and prints which block each nonce landed in. Use a throwaway key holding a
little gas; it refuses the deployment's admin key, because a second writer on
the admin nonce while the API runs would collide with live sends.
"""

import os
import sys
import time

from eth_account import Account
from web3 import Web3

from agentpit.onchain.deployment import Deployment
from agentpit.onchain.tx_sender import AdminTxSender, Web3ChainRpc
from agentpit.onchain.web3_client import build_http_provider


def main() -> int:
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    account = Account.from_key(os.environ["MTM_SMOKE_PK"])
    deployment_path = os.environ.get("AGENTPIT_DEPLOYMENT_PATH", "deployments/local.json")
    if os.path.exists(deployment_path):
        if Deployment.load(deployment_path).admin.lower() == account.address.lower():
            print("refusing to run with the deployment's admin key", file=sys.stderr)
            return 2
    w3 = Web3(build_http_provider(os.environ["RPC_URL"]))
    sender = AdminTxSender(Web3ChainRpc(w3), account, w3.eth.chain_id, max_in_flight=count)
    print(f"sender {account.address}  balance {w3.eth.get_balance(account.address)} wei")
    started = time.monotonic()
    pendings = [sender.submit_value(account.address, 0) for _ in range(count)]
    submitted = time.monotonic() - started
    receipts = sender.wait_all(pendings, timeout=120)
    elapsed = time.monotonic() - started
    blocks: dict[int, list[int]] = {}
    for p, r in zip(pendings, receipts):
        if isinstance(r, Exception):
            print(f"nonce {p.nonce}: {r!r}")
            continue
        blocks.setdefault(r["blockNumber"], []).append(p.nonce)
    for block, nonces in sorted(blocks.items()):
        print(f"block {block}: nonces {nonces}")
    print(
        f"{count} txs submitted in {submitted:.2f}s, mined in {elapsed:.2f}s "
        f"across {len(blocks)} block(s)"
    )
    return 0 if len(blocks) < count else 1


if __name__ == "__main__":
    sys.exit(main())
