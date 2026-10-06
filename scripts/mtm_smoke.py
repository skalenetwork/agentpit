"""Check, against a live chain, that one key lands several transactions per block.

    MTM_SMOKE_PK=0x... RPC_URL=https://... python scripts/mtm_smoke.py [--batch] [count]

Sends `count` (default 10) 0-value transfers to itself through AdminTxSender
and prints which block each nonce landed in. One `submit_value` per transfer,
or with `--batch` one `submit_values`: JSON-RPC batches of up to 100 (expect
100 transfers in one or two blocks). Use a throwaway key holding a little gas;
it refuses the deployment's admin key, because a second writer on the admin
nonce while the API runs would collide with live sends.
"""

import argparse
import os
import sys
import time

from eth_account import Account
from web3 import Web3

from agentpit.onchain.deployment import Deployment
from agentpit.onchain.tx_sender import AdminTxSender, Web3ChainRpc
from agentpit.onchain.web3_client import build_http_provider


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("count", nargs="?", type=int, default=10)
    parser.add_argument(
        "--batch", action="store_true", help="send them with submit_values"
    )
    args = parser.parse_args()
    count = args.count
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
    if args.batch:
        results = sender.submit_values([(account.address, 0)] * count)
        for n, r in enumerate(results):
            if isinstance(r, Exception):
                print(f"transfer {n}: not sent: {r!r}")
        pendings = [r for r in results if not isinstance(r, Exception)]
    else:
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
    mode = "in batches" if args.batch else "one by one"
    print(
        f"{len(pendings)}/{count} txs submitted {mode} in {submitted:.2f}s, "
        f"mined in {elapsed:.2f}s across {len(blocks)} block(s)"
    )
    return 0 if len(pendings) == count and len(blocks) < count else 1


if __name__ == "__main__":
    sys.exit(main())
