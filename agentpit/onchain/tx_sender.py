"""Pipelined, nonce-managed transaction sender for the admin key.

SKALE runs our chain in Multi-Transaction Mode: one account may have many
transactions in one block, provided the client hands them over without
waiting for each receipt. The old `send_admin_tx` held one lock from the nonce
read to the receipt, so the admin key landed exactly one transaction per block
(~4.6 s each) and the market sync, user settlement (`matchOrders`), faucet and
gas grants all queued behind one another.

skaled behaviour this relies on (tag 5.2.0-beta.1, checked 2026-10-06):
- `eth_getTransactionCount(addr, "pending")` is the COMMITTED nonce: queued
  transactions are invisible, so the next nonce is counted here, locally.
- `eth_estimateGas` runs on committed state and ignores the nonce field.
- A second transaction on a taken nonce is refused; there is no replace-by-fee.
- A nonce gap parks every later nonce until the gap is filled.
- With MTM off, a nonce above the committed one is refused.

Only nonce, sign and broadcast happen under the send lock (one round trip);
receipts are polled outside it, up to 100 hashes per JSON-RPC batch.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from eth_account.signers.local import LocalAccount
from hexbytes import HexBytes
from web3 import Web3
from web3._utils.method_formatters import receipt_formatter
from web3.contract.contract import ContractFunction
from web3.datastructures import AttributeDict
from web3.exceptions import TimeExhausted, TransactionNotFound
from web3.types import TxReceipt

log = logging.getLogger(__name__)

# SKALE refuses a JSON-RPC batch above 128 requests.
_RPC_BATCH = 100
_TRANSFER_GAS = 21_000
# How long an estimate that reverted waits for our in-flight txs to land.
_DRAIN_TIMEOUT_S = 60.0
# Receipts nobody collected (their waiter timed out) are forgotten after this.
_FORGET_DONE_AFTER_S = 600.0


class ChainRpc(Protocol):
    """The node calls the sender makes. `Web3ChainRpc` in production, a fake
    skaled in tests."""

    def nonce(self, address: str, block: str) -> int: ...

    def send_raw(self, raw: bytes) -> None: ...

    def tx_known(self, tx_hash: bytes) -> bool: ...

    def receipts(self, tx_hashes: list[bytes]) -> list[TxReceipt | None]: ...

    def estimate_gas(self, tx: dict) -> int: ...

    def fee_params(self) -> tuple[int, int]: ...


class Web3ChainRpc:
    """`ChainRpc` over a web3 HTTP provider."""

    def __init__(self, web3: Web3):
        self._w3 = web3

    def nonce(self, address: str, block: str) -> int:
        return self._w3.eth.get_transaction_count(address, block)  # type: ignore[arg-type]

    def send_raw(self, raw: bytes) -> None:
        self._w3.eth.send_raw_transaction(raw)

    def tx_known(self, tx_hash: bytes) -> bool:
        try:
            self._w3.eth.get_transaction(HexBytes(tx_hash))
        except TransactionNotFound:
            return False
        return True

    def receipts(self, tx_hashes: list[bytes]) -> list[TxReceipt | None]:
        """Receipts for many hashes in one round trip per 100; None = not mined."""
        out: list[TxReceipt | None] = []
        for start in range(0, len(tx_hashes), _RPC_BATCH):
            chunk = tx_hashes[start : start + _RPC_BATCH]
            responses = self._w3.provider.make_batch_request(
                [("eth_getTransactionReceipt", [Web3.to_hex(h)]) for h in chunk]  # type: ignore[misc]
            )
            if not isinstance(responses, list):
                raise RuntimeError(f"receipt batch failed: {responses}")
            for response in responses:
                if response.get("error"):
                    raise RuntimeError(
                        f"receipt batch item failed: {response['error']}"
                    )
                raw = response.get("result")
                out.append(
                    None
                    if raw is None
                    else AttributeDict.recursive(receipt_formatter(raw))
                )
        return out

    def estimate_gas(self, tx: dict) -> int:
        return self._w3.eth.estimate_gas(tx)  # type: ignore[arg-type]

    def fee_params(self) -> tuple[int, int]:
        """(maxFeePerGas, maxPriorityFeePerGas) as web3's own defaults compute
        them: twice the latest base fee plus the node's suggested tip."""
        priority = self._w3.eth.max_priority_fee
        base = self._w3.eth.get_block("latest").get("baseFeePerGas") or 0
        if not base:
            return self._w3.eth.gas_price, 0
        return 2 * base + priority, priority


@dataclass(frozen=True)
class PendingTx:
    """A transaction the node accepted. Wait on it once, with
    `AdminTxSender.wait` or `wait_all`."""

    tx_hash: bytes
    nonce: int


class TxDropped(RuntimeError):
    """The node lost the transaction and a gap filler took its nonce, so it
    can never run."""


class _Entry:
    __slots__ = ("pending", "sent_at", "receipt", "superseded_by", "dropped", "done_at")

    def __init__(self, pending: PendingTx, sent_at: float):
        self.pending = pending
        self.sent_at = sent_at
        self.receipt: TxReceipt | None = None
        self.superseded_by: bytes | None = None
        self.dropped = False
        self.done_at: float | None = None

    @property
    def done(self) -> bool:
        return self.receipt is not None or self.dropped


class AdminTxSender:
    """Sends the admin key's transactions with many of them in flight.

    One instance per process and key: the next nonce lives here. Callers
    either `send` (blocks until mined, like the old `send_admin_tx`) or
    `submit` now and `wait_all` later, which is how the sync puts a whole
    chunk of markets into one or two blocks.
    """

    def __init__(
        self,
        rpc: ChainRpc,
        account: LocalAccount,
        chain_id: int,
        *,
        max_in_flight: int = 64,
        poll_interval: float = 0.25,
        fee_ttl: float = 30.0,
        stall_after: float = 15.0,
        slot_timeout: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if max_in_flight < 1:
            raise ValueError("max_in_flight must be at least 1")
        self._rpc = rpc
        self._account = account
        self._chain_id = chain_id
        self._max_in_flight = max_in_flight
        self._poll_interval = poll_interval
        self._fee_ttl = fee_ttl
        self._stall_after = stall_after
        # How long one submit may wait, queueing on the send lock included,
        # for a free in-flight slot before giving up.
        self._slot_timeout = slot_timeout
        self._clock = clock
        self._sleep = sleep
        # nonce + sign + broadcast. Re-entrant because healing a stalled nonce
        # (Task 4) sends from inside a poll that a sender may already be in.
        self._send_lock = threading.RLock()
        self._state = threading.Lock()  # guards _entries
        self._reap_lock = threading.Lock()  # one receipt poll at a time
        self._entries: dict[bytes, _Entry] = {}
        self._next_nonce: int | None = None
        self._fees: tuple[int, int, float] | None = None
        self._last_reap = float("-inf")

    @property
    def address(self) -> str:
        return self._account.address

    @property
    def max_in_flight(self) -> int:
        return self._max_in_flight

    # --- sending ----------------------------------------------------

    def submit(
        self, fn: ContractFunction, *, gas: int | None = None, gas_buffer_pct: int = 20
    ) -> PendingTx:
        """Broadcast `fn(...)` and return without waiting for it to mine.

        `gas` is a static limit; without one the node's estimate plus
        `gas_buffer_pct` is used. An estimate runs on committed state, so a
        transaction that depends on one of ours still in flight should pass
        a static limit.
        """
        # Every field filled in, so web3 builds this without a single RPC.
        built = fn.build_transaction(
            {
                "from": self.address,
                "gas": 1,
                "maxFeePerGas": 1,
                "maxPriorityFeePerGas": 0,
                "nonce": 0,
                "chainId": self._chain_id,
            }
        )
        base = {
            "to": built["to"],
            "data": built["data"],
            "value": built.get("value", 0),
        }
        return self._submit(base, gas, gas_buffer_pct)

    def submit_value(self, to: str, value_wei: int) -> PendingTx:
        """Broadcast a plain native-token transfer."""
        base = {"to": Web3.to_checksum_address(to), "data": b"", "value": value_wei}
        return self._submit(base, _TRANSFER_GAS, 0)

    def send(
        self,
        fn: ContractFunction,
        *,
        timeout: float,
        gas: int | None = None,
        gas_buffer_pct: int = 20,
    ) -> TxReceipt:
        """`submit` then `wait`: the receipt whatever its status."""
        return self.wait(
            self.submit(fn, gas=gas, gas_buffer_pct=gas_buffer_pct), timeout=timeout
        )

    def send_value(self, to: str, value_wei: int, *, timeout: float) -> TxReceipt:
        return self.wait(self.submit_value(to, value_wei), timeout=timeout)

    def _submit(self, base: dict, gas: int | None, gas_buffer_pct: int) -> PendingTx:
        if gas is None:
            gas = self._estimate(base) * (100 + gas_buffer_pct) // 100
        # One deadline for the whole wait, set before queueing on the send
        # lock: submitters queued behind each other share it instead of each
        # starting its own once it finally holds the lock.
        deadline = self._clock() + self._slot_timeout
        if not self._send_lock.acquire(timeout=max(0.0, deadline - self._clock())):
            raise TimeExhausted(
                f"no free admin transaction slot after {self._slot_timeout:g}s "
                "(queued behind other submitters)"
            )
        try:
            self._wait_for_slot(deadline)
            return self._sign_and_send(base, gas)
        finally:
            self._send_lock.release()

    def _estimate(self, base: dict) -> int:
        tx = {"from": self.address, **base}
        try:
            return self._rpc.estimate_gas(tx)
        except Exception:
            # Estimates run on committed state. A transaction that depends on
            # one of ours still in flight can revert here though it would not
            # on chain; the old serial sender never met this because it only
            # estimated after the previous transaction had mined. Let ours
            # land, then ask once more. Only the ones in flight right now: a
            # steady stream of new sends would keep the count above zero and
            # a genuinely reverting estimate would wait out the whole bound.
            in_flight = self._in_flight_hashes()
            if not in_flight:
                raise
            self._wait_until_done(in_flight, _DRAIN_TIMEOUT_S)
            return self._rpc.estimate_gas(tx)

    def _sign_and_send(self, base: dict, gas: int) -> PendingTx:
        """Under the send lock: take the next nonce, sign, broadcast.

        Any error from the broadcast means the node refused the transaction,
        so the nonce is not consumed and the error propagates.
        """
        if self._next_nonce is None:
            # "pending" equals the committed nonce on skaled; on anvil/geth it
            # also counts our own transactions still in the pool.
            self._next_nonce = self._rpc.nonce(self.address, "pending")
        nonce = self._next_nonce
        signed = self._sign(base, nonce, gas)
        self._rpc.send_raw(bytes(signed.raw_transaction))
        return self._accept(bytes(signed.hash), nonce)

    def _sign(self, base: dict, nonce: int, gas: int):
        max_fee, priority = self._fee_params()
        return self._account.sign_transaction(
            {
                **base,
                "nonce": nonce,
                "gas": gas,
                "maxFeePerGas": max_fee,
                "maxPriorityFeePerGas": priority,
                "chainId": self._chain_id,
                "type": 2,
            }
        )

    def _accept(self, tx_hash: bytes, nonce: int) -> PendingTx:
        pending = PendingTx(tx_hash=tx_hash, nonce=nonce)
        with self._state:
            self._entries[tx_hash] = _Entry(pending, self._clock())
        self._next_nonce = nonce + 1
        return pending

    def _fee_params(self) -> tuple[int, int]:
        now = self._clock()
        if self._fees is None or now - self._fees[2] > self._fee_ttl:
            max_fee, priority = self._rpc.fee_params()
            self._fees = (max_fee, priority, now)
        return self._fees[0], self._fees[1]

    def _wait_for_slot(self, deadline: float | None = None) -> None:
        if deadline is None:
            deadline = self._clock() + self._slot_timeout
        while self._in_flight_count() >= self._max_in_flight:
            if self._clock() >= deadline:
                raise TimeExhausted(
                    f"no free admin transaction slot after {self._slot_timeout:g}s "
                    f"({self._max_in_flight} in flight)"
                )
            self.poll()
            self._sleep(self._poll_interval)

    def _wait_until_done(self, tx_hashes: set[bytes], timeout: float) -> None:
        """Poll until every one of `tx_hashes` has a receipt or was dropped,
        or `timeout` passes. One already collected by its waiter is gone from
        `_entries` and counts as done."""
        deadline = self._clock() + timeout
        while self._any_in_flight(tx_hashes) and self._clock() < deadline:
            self.poll()
            self._sleep(self._poll_interval)

    def _any_in_flight(self, tx_hashes: set[bytes]) -> bool:
        with self._state:
            return any(
                e is not None and not e.done for e in map(self._entries.get, tx_hashes)
            )

    def _in_flight_hashes(self) -> set[bytes]:
        with self._state:
            return {h for h, e in self._entries.items() if not e.done}

    def _in_flight_count(self) -> int:
        with self._state:
            return sum(1 for e in self._entries.values() if not e.done)

    # --- waiting ----------------------------------------------------

    def wait(self, pending: PendingTx, *, timeout: float) -> TxReceipt:
        """Block until `pending` mines and return its receipt, whatever its
        status. Raises `TimeExhausted` after `timeout`, `TxDropped` if a gap
        filler took its nonce."""
        result = self.wait_all([pending], timeout=timeout)[0]
        if isinstance(result, Exception):
            raise result
        return result

    def wait_all(
        self, pendings: list[PendingTx], *, timeout: float
    ) -> list[TxReceipt | Exception]:
        """One result per input, in order: the receipt, or the exception
        `wait` would have raised for it."""
        deadline = self._clock() + timeout
        results: dict[bytes, TxReceipt | Exception] = {}
        wanted = {p.tx_hash for p in pendings}
        while True:
            with self._state:
                for tx_hash in wanted - results.keys():
                    entry = self._entries.get(tx_hash)
                    if entry is None:
                        results[tx_hash] = KeyError(
                            f"transaction {HexBytes(tx_hash).to_0x_hex()} is not tracked"
                        )
                    elif entry.receipt is not None:
                        results[tx_hash] = entry.receipt
                        del self._entries[tx_hash]
                    elif entry.dropped:
                        results[tx_hash] = TxDropped(
                            f"transaction {HexBytes(tx_hash).to_0x_hex()} (nonce "
                            f"{entry.pending.nonce}) was dropped by the node; its "
                            "nonce went to a filler and it never ran"
                        )
                        del self._entries[tx_hash]
            if len(results) == len(wanted):
                break
            if self._clock() >= deadline:
                for tx_hash in wanted - results.keys():
                    results[tx_hash] = TimeExhausted(
                        f"Transaction {HexBytes(tx_hash).to_0x_hex()} is not in the "
                        f"chain after {timeout} seconds"
                    )
                break
            self.poll()
            self._sleep(self._poll_interval)
        return [results[p.tx_hash] for p in pendings]

    def poll(self) -> None:
        """Fetch receipts for everything in flight, one batch per 100."""
        self._reap()

    def _reap(self) -> None:
        if not self._reap_lock.acquire(blocking=False):
            return  # another thread is polling; its results land in _entries
        try:
            now = self._clock()
            if now - self._last_reap < self._poll_interval / 2:
                return
            self._last_reap = now
            with self._state:
                for tx_hash in [
                    h
                    for h, e in self._entries.items()
                    if e.done_at is not None and now - e.done_at > _FORGET_DONE_AFTER_S
                ]:
                    del self._entries[tx_hash]
                waiting = [e for e in self._entries.values() if not e.done]
            if not waiting:
                return
            try:
                receipts = self._rpc.receipts([e.pending.tx_hash for e in waiting])
            except Exception as exc:
                log.warning("admin receipt poll failed: %s", exc)
                return
            with self._state:
                for entry, receipt in zip(waiting, receipts):
                    if receipt is not None:
                        entry.receipt = receipt
                        entry.done_at = now
        finally:
            self._reap_lock.release()
