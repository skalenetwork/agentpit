"""Just enough of skaled's transaction pool to test AdminTxSender offline.

Models the rules the sender depends on (skaled 5.2.0-beta.1):
- `nonce()` is the COMMITTED nonce whatever the block tag; queued txs are
  invisible to it.
- With MTM on, any nonce >= committed is accepted and waits until the gap
  below it is filled; with MTM off only nonce == committed is accepted.
- A transaction priced under the current price (`fee`) is refused, before
  its nonce or the queue is looked at.
- A taken nonce is refused (no replace-by-fee); a duplicate hash is refused.
- `mine()` cuts one block holding every contiguous nonce per sender.
- A JSON-RPC batch of `eth_sendRawTransaction` is imported one item after
  another, each item answered on its own.
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
        # nonce -> refused with this answer before anything else is checked
        # (as skaled checks the fee first), once.
        self.refuse: dict[int, Exception] = {}
        # nonce -> imported, but answered with this error instead of OK, once.
        self.answer_lost: dict[int, Exception] = {}
        # Raised by the next send_raw_batch calls BEFORE any item is imported.
        self.batch_errors: list[Exception] = []
        # Raised by the next send_raw_batch calls AFTER every item was imported.
        self.batch_accept_then_raise: list[Exception] = []
        self.batches: list[list[bytes]] = []  # the raws of every batch call
        self.revert: set[int] = set()  # nonces mined with status 0
        self.estimate_errors: list[Exception] = []
        self.pending_errors: list[Exception] = []  # raised by pending_hashes
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
            self._import(raw)
            if self.accept_then_raise:
                raise self.accept_then_raise.pop(0)

    def send_raw_batch(self, raws: list[bytes]) -> list[Exception | None]:
        """skaled imports a batch's items one after another; each gets its own
        answer, None for OK."""
        with self._lock:
            self.batches.append([bytes(r) for r in raws])
            if self.batch_errors:
                raise self.batch_errors.pop(0)
            answers: list[Exception | None] = []
            for raw in raws:
                try:
                    self._import(raw)
                except Exception as exc:  # noqa: BLE001 - the item's answer
                    answers.append(exc)
                else:
                    answers.append(None)
            if self.batch_accept_then_raise:
                raise self.batch_accept_then_raise.pop(0)
            return answers

    def _import(self, raw: bytes) -> None:
        tx = TypedTransaction.from_bytes(HexBytes(raw)).as_dict()
        sender = Account.recover_transaction(raw)
        nonce = tx["nonce"]
        tx_hash = keccak(raw)
        if nonce in self.refuse:
            raise self.refuse.pop(nonce)
        # skaled checks the price first (verifyTransaction): under the current
        # eth_gasPrice is refused even when the node holds these very bytes.
        if tx["maxFeePerGas"] < self.fee[0]:
            raise FakeRpcError("Transaction gas price lower than current eth_gasPrice.")
        # skaled verifies the nonce BEFORE it looks at its queue
        # (Client::importTransaction), so a mined transaction sent again
        # is "Invalid transaction nonce", not "already in the blockchain".
        base = self.committed[sender]
        if nonce < base or (not self.mtm and nonce != base):
            raise FakeRpcError("Invalid transaction nonce.")
        if tx_hash in self.mined:
            raise FakeRpcError("Transaction is already in the blockchain.")
        if any(q["hash"] == tx_hash for q in self.queue):
            raise FakeRpcError(
                "Same transaction already exists in the pending transaction queue."
            )
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
        if nonce in self.answer_lost:
            raise self.answer_lost.pop(nonce)

    def pending_hashes(self) -> set[bytes]:
        """skaled's `eth_pendingTransactions`: the CURRENT queue only, i.e.
        each sender's transactions that are contiguous from its committed
        nonce (`Client::pending` is `topTransactions(status().current)`). A
        transaction parked behind a gap is in the future queue: not listed."""
        with self._lock:
            if self.pending_errors:
                raise self.pending_errors.pop(0)
            out: set[bytes] = set()
            by_sender: dict[str, dict[int, bytes]] = defaultdict(dict)
            for q in self.queue:
                by_sender[q["sender"]][q["nonce"]] = q["hash"]
            for sender, nonces in by_sender.items():
                nonce = self.committed[sender]
                while nonce in nonces:
                    out.add(nonces[nonce])
                    nonce += 1
            return out

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

    def mine(self, limit: int | None = None) -> int:
        """Cut one block: every queued tx whose nonce is next for its sender
        (the first `limit` of them, if given)."""
        with self._lock:
            self.block += 1
            count = 0
            progress = True
            while progress:
                progress = False
                for item in sorted(self.queue, key=lambda q: q["nonce"]):
                    if limit is not None and count >= limit:
                        break
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
