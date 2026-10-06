"""Just enough of skaled's transaction pool to test AdminTxSender offline.

Models the rules the sender depends on (skaled 5.2.0-beta.1):
- `nonce()` is the COMMITTED nonce whatever the block tag; queued txs are
  invisible to it.
- With MTM on, any nonce >= committed is accepted and waits until the gap
  below it is filled; with MTM off only nonce == committed is accepted.
- A taken nonce is refused (no replace-by-fee); a duplicate hash is refused.
- `mine()` cuts one block holding every contiguous nonce per sender.
Knobs let a test make the node refuse, lose, revert or half-answer a send.
"""

import threading
from collections import defaultdict

from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from eth_utils import keccak
from hexbytes import HexBytes
from web3.datastructures import AttributeDict


class FakeRpcError(Exception):
    """What a node's JSON-RPC error looks like to the sender: a message."""


class FakeSkaled:
    def __init__(self, *, mtm: bool = True):
        self.mtm = mtm
        self.committed: dict[str, int] = defaultdict(int)
        self.queue: list[dict] = []  # accepted, not mined: {sender, nonce, hash, tx}
        self.mined: dict[bytes, AttributeDict] = {}
        self.block = 0
        self.accepted: list[dict] = []  # every accepted tx, in arrival order
        # Raised by the next send_raw calls BEFORE the node looks at the tx.
        self.send_errors: list[Exception] = []
        # Raised by the next send_raw calls AFTER the tx was accepted (lost answer).
        self.accept_then_raise: list[Exception] = []
        self.lose: set[int] = set()  # nonces accepted with OK but never queued
        self.revert: set[int] = set()  # nonces mined with status 0
        self.estimate_errors: list[Exception] = []
        self.known_errors: list[Exception] = []  # raised by tx_known
        self.fee = (200_000, 0)
        self.fee_calls = 0
        self.estimate_calls = 0
        self.nonce_calls = 0
        self._lock = threading.RLock()

    # --- ChainRpc ---------------------------------------------------

    def nonce(self, address: str, block: str) -> int:
        with self._lock:
            self.nonce_calls += 1
            return self.committed[address]

    def send_raw(self, raw: bytes) -> None:
        with self._lock:
            if self.send_errors:
                raise self.send_errors.pop(0)
            tx = TypedTransaction.from_bytes(HexBytes(raw)).as_dict()
            sender = Account.recover_transaction(raw)
            nonce = tx["nonce"]
            tx_hash = keccak(raw)
            if tx_hash in self.mined:
                raise FakeRpcError("Transaction is already in the blockchain.")
            if any(q["hash"] == tx_hash for q in self.queue):
                raise FakeRpcError(
                    "Same transaction already exists in the pending transaction queue."
                )
            base = self.committed[sender]
            if nonce < base or (not self.mtm and nonce != base):
                raise FakeRpcError("Invalid transaction nonce.")
            if any(q["sender"] == sender and q["nonce"] == nonce for q in self.queue):
                raise FakeRpcError(
                    "Pending transaction with same nonce already exists "
                    "(skale: we ignore gas price)."
                )
            item = {"sender": sender, "nonce": nonce, "hash": tx_hash, "tx": tx}
            self.accepted.append(item)
            if nonce in self.lose:
                self.lose.discard(nonce)
            else:
                self.queue.append(item)
            if self.accept_then_raise:
                raise self.accept_then_raise.pop(0)

    def tx_known(self, tx_hash: bytes) -> bool:
        with self._lock:
            if self.known_errors:
                raise self.known_errors.pop(0)
            return tx_hash in self.mined or any(
                q["hash"] == tx_hash for q in self.queue
            )

    def receipts(self, tx_hashes: list[bytes]) -> list:
        with self._lock:
            return [self.mined.get(bytes(h)) for h in tx_hashes]

    def estimate_gas(self, tx: dict) -> int:
        with self._lock:
            self.estimate_calls += 1
            if self.estimate_errors:
                raise self.estimate_errors.pop(0)
            return 50_000

    def fee_params(self) -> tuple[int, int]:
        with self._lock:
            self.fee_calls += 1
            return self.fee

    # --- test controls ----------------------------------------------

    def mine(self) -> int:
        """Cut one block: every queued tx whose nonce is next for its sender."""
        with self._lock:
            self.block += 1
            count = 0
            progress = True
            while progress:
                progress = False
                for item in sorted(self.queue, key=lambda q: q["nonce"]):
                    if item["nonce"] != self.committed[item["sender"]]:
                        continue
                    self.queue.remove(item)
                    self.mined[item["hash"]] = AttributeDict(
                        {
                            "transactionHash": HexBytes(item["hash"]),
                            "status": 0 if item["nonce"] in self.revert else 1,
                            "blockNumber": self.block,
                            "nonce": item["nonce"],
                            "gas": item["tx"]["gas"],
                        }
                    )
                    self.committed[item["sender"]] += 1
                    count += 1
                    progress = True
            return count

    def inject(self, account, nonce: int) -> bytes:
        """Queue a 0-value transfer from `account` at `nonce`, as if another
        process holding the same key had sent it."""
        signed = account.sign_transaction(
            {
                "to": account.address,
                "value": 0,
                "nonce": nonce,
                "gas": 21_000,
                "maxFeePerGas": 200_000,
                "maxPriorityFeePerGas": 0,
                "chainId": 1,
                "type": 2,
            }
        )
        self.send_raw(bytes(signed.raw_transaction))
        return bytes(signed.hash)

    def blocks_of(self, pendings) -> list[int]:
        with self._lock:
            return [self.mined[p.tx_hash]["blockNumber"] for p in pendings]


class FakeClock:
    def __init__(self, start: float = 1_000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


CHAIN_ID = 1


class FakeFn:
    """Stands in for a web3 ContractFunction: build_transaction with every
    field filled makes no RPC, so the sender only needs to/data/value."""

    address = "0x000000000000000000000000000000000000dEaD"

    def build_transaction(self, tx):
        return {**tx, "to": self.address, "data": "0x1234", "value": 0}


def make_sender(chain, *, mine_on_sleep=True, **kw):
    """An AdminTxSender on `chain` with a fake clock whose every sleep is
    one block (unless `mine_on_sleep=False`). Returns (sender, account, clock)."""
    from agentpit.onchain.tx_sender import AdminTxSender

    clock = FakeClock()

    def sleep(seconds):
        clock.advance(seconds)
        if mine_on_sleep:
            chain.mine()

    account = Account.create()
    sender = AdminTxSender(chain, account, CHAIN_ID, clock=clock, sleep=sleep, **kw)
    return sender, account, clock
