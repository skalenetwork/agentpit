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
- `eth_getTransactionByHash` finds only MINED transactions; a queued one
  answers null (libweb3jsonrpc/Eth.cpp, ClientBase.cpp). The queue is visible
  in `eth_pendingTransactions`, but only its current part: a transaction
  parked behind a nonce gap is not listed (Client::pending).

Nonce, sign and broadcast happen under the send lock, normally one round trip;
the retries of a refused send, the resend after a lost answer and healing a
stalled nonce run under it too. Receipts are polled outside it, up to 100
hashes per JSON-RPC batch.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

import requests
from eth_account.signers.local import LocalAccount
from hexbytes import HexBytes
from urllib3.exceptions import ConnectTimeoutError, MaxRetryError
from web3 import Web3
from web3._utils.method_formatters import receipt_formatter
from web3.contract.contract import ContractFunction
from web3.datastructures import AttributeDict
from web3.exceptions import RequestTimedOut, TimeExhausted
from web3.types import RPCEndpoint, TxReceipt

log = logging.getLogger(__name__)

# SKALE refuses a JSON-RPC batch above 128 requests.
_RPC_BATCH = 100
_TRANSFER_GAS = 21_000
# How long an estimate that reverted waits for our in-flight txs to land.
_DRAIN_TIMEOUT_S = 60.0
# Receipts nobody collected (their waiter timed out) are forgotten after this.
_FORGET_DONE_AFTER_S = 600.0
# How long a transaction may sit unmined before the sender checks whether the
# node lost it.
_STALL_AFTER_S = 15.0
# Receipt lookups, `poll_interval` apart, for a transaction whose resend was
# answered "invalid nonce" (it may have mined, with its receipt not yet served).
_OWN_RECEIPT_LOOKUPS = 3


class SendError(Enum):
    DUPLICATE = "duplicate"  # the node already holds this very transaction
    NONCE_TAKEN = "nonce_taken"  # another transaction holds the nonce
    NONCE_INVALID = "nonce_invalid"  # below committed, or above it with MTM off
    FEE_LOW = "fee_low"
    TRANSPORT = "transport"  # no answer: the node may or may not hold it
    OTHER = "other"  # any other answer: refused


# Lowercase substrings of skaled's answers, plus geth/anvil wording. Checked in
# this order, so "replacement transaction underpriced" is NONCE_TAKEN, not
# FEE_LOW.
_ANSWERS = (
    (
        SendError.DUPLICATE,
        (
            "same transaction already exists",
            "already in the blockchain",
            "already known",
            "known transaction",
            "transaction already imported",
        ),
    ),
    (
        SendError.NONCE_TAKEN,
        ("same nonce already exists", "replacement transaction underpriced"),
    ),
    (SendError.NONCE_INVALID, ("invalid transaction nonce", "nonce too low")),
    (
        SendError.FEE_LOW,
        (
            "lower than current eth_gasprice",
            "less than block base fee",
            "transaction underpriced",
        ),
    ),
)


def classify_send_error(exc: BaseException) -> SendError:
    """What a failed `eth_sendRawTransaction` says about the transaction."""
    if isinstance(
        exc,
        (
            requests.ConnectionError,
            requests.Timeout,
            requests.exceptions.ChunkedEncodingError,  # the body was cut
            ConnectionError,
            TimeoutError,
            RequestTimedOut,
        ),
    ):
        return SendError.TRANSPORT
    if isinstance(exc, requests.HTTPError):
        # A proxy's 502/503/504 says nothing about the node: the request may
        # have reached it. Any other status (429, 4xx) means it was not taken.
        # `is not None`: a Response is falsy for exactly these statuses.
        response = exc.response
        if response is not None and response.status_code >= 500:
            return SendError.TRANSPORT
    text = str(exc).lower()
    for kind, markers in _ANSWERS:
        if any(marker in text for marker in markers):
            return kind
    return SendError.OTHER


def failed_before_connecting(exc: BaseException) -> bool:
    """Did this failed request provably never reach the node?

    Only a failure to open the connection says so: a refused or timed-out TCP
    connect, or a host name that did not resolve. requests raises those as
    `ConnectTimeout`, or as a `ConnectionError` wrapping urllib3's
    `MaxRetryError` whose reason is a `NewConnectionError` (refused, DNS) or a
    `ConnectTimeoutError`. A read timeout, or a reset or a hang-up after
    connecting, may come after the node took the request: not this.

    Only the exception requests wrapped (its first argument, or an explicit
    `raise ... from`) is looked at, never `__context__` or anything deeper: a
    reset raised while an earlier refusal was being handled carries that
    refusal in its context, yet its own request did connect. A wrong True
    would report a send the node may hold as refused, so every doubt is
    False.
    """
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return True
    if not isinstance(exc, requests.exceptions.ConnectionError):
        return False
    wrapped = next((a for a in exc.args if isinstance(a, BaseException)), None)
    if wrapped is None:
        wrapped = exc.__cause__
    if isinstance(wrapped, MaxRetryError):
        wrapped = wrapped.reason
    # NewConnectionError (and its NameResolutionError) is a ConnectTimeoutError.
    return isinstance(wrapped, ConnectTimeoutError)


class ChainRpc(Protocol):
    """The node calls the sender makes. `Web3ChainRpc` in production, a fake
    skaled in tests."""

    def nonce(self, address: str, block: str) -> int: ...

    def send_raw(self, raw: bytes) -> None: ...

    def pending_hashes(self) -> set[bytes]: ...

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

    def pending_hashes(self) -> set[bytes]:
        """Hashes in the node's queue, in one read of `eth_pendingTransactions`.

        skaled's `eth_getTransactionByHash` finds only mined transactions, so
        this list is the only way to see a queued one. It is the CURRENT
        queue only (`Client::pending` is `topTransactions(status().current)`):
        a transaction parked behind a nonce gap is not in it. If the list
        cannot be read the answer is empty; the stall healer copes with a
        wrong "not queued" (a filler on a nonce the node holds is refused).
        """
        try:
            response = self._w3.provider.make_request(
                RPCEndpoint("eth_pendingTransactions"), []
            )
            queued = response.get("result")
            if not isinstance(queued, list):
                return set()
            return {
                bytes(HexBytes(tx["hash"]))
                for tx in queued
                if isinstance(tx, dict) and tx.get("hash")
            }
        except Exception:
            return set()

    def receipts(self, tx_hashes: list[bytes]) -> list[TxReceipt | None]:
        """Receipts for many hashes in one round trip per 100; None = not mined.

        Each receipt is matched to the hash it names in `transactionHash`,
        never by its position: a short answer or a failed item must not shift
        receipts onto other hashes. A hash whose item is missing, failed or
        names another hash reads as not mined for this poll, with one warning
        per call, so one bad item never drops the poll for the others.
        """
        out: list[TxReceipt | None] = []
        problems: list[str] = []
        for start in range(0, len(tx_hashes), _RPC_BATCH):
            chunk = [bytes(h) for h in tx_hashes[start : start + _RPC_BATCH]]
            responses = self._w3.provider.make_batch_request(
                [("eth_getTransactionReceipt", [Web3.to_hex(h)]) for h in chunk]  # type: ignore[misc]
            )
            if not isinstance(responses, list):
                raise RuntimeError(f"receipt batch failed: {responses}")
            out.extend(_match_receipts(chunk, responses, problems))
        if problems:
            log.warning(
                "admin receipt batch answered oddly, those hashes read as not "
                "mined this poll: %s",
                "; ".join(problems[:5]) + ("; ..." if len(problems) > 5 else ""),
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


def _match_receipts(
    hashes: list[bytes], responses: list, problems: list[str]
) -> list[TxReceipt | None]:
    """One receipt or None per hash in `hashes`, from one batch's answers,
    each placed by the hash it names. What does not fit goes to `problems`."""
    if len(responses) != len(hashes):
        problems.append(f"{len(responses)} answers for {len(hashes)} hashes")
    wanted = set(hashes)
    found: dict[bytes, TxReceipt] = {}
    for response in responses:
        if not isinstance(response, dict) or response.get("error"):
            error = response.get("error") if isinstance(response, dict) else response
            problems.append(f"item failed: {error}")
            continue
        raw = response.get("result")
        if raw is None:
            continue  # not mined yet; nothing says which hash, nothing to place
        try:
            tx_hash = bytes(HexBytes(raw["transactionHash"]))
        except (KeyError, TypeError, ValueError):
            problems.append("a receipt without a transactionHash")
            continue
        if tx_hash not in wanted:
            problems.append(f"a receipt for {HexBytes(tx_hash).to_0x_hex()}, not asked")
            continue
        found[tx_hash] = AttributeDict.recursive(receipt_formatter(raw))
    return [found.get(h) for h in hashes]


@dataclass(frozen=True)
class PendingTx:
    """A transaction the node accepted. Wait on it once, with
    `AdminTxSender.wait` or `wait_all`."""

    tx_hash: bytes
    nonce: int


class TxDropped(RuntimeError):
    """The transaction can never run: its nonce went to a gap filler (the node
    lost it) or to another writer's transaction."""


class _Entry:
    __slots__ = (
        "pending",
        "sent_at",
        "receipt",
        "superseded_by",
        "dropped",
        "done_at",
        "suspect_since",
    )

    def __init__(self, pending: PendingTx, sent_at: float):
        self.pending = pending
        self.sent_at = sent_at
        self.receipt: TxReceipt | None = None
        self.superseded_by: bytes | None = None
        self.dropped = False
        self.done_at: float | None = None
        # When a heal first found its nonce used but no receipt for it.
        self.suspect_since: float | None = None

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
        stall_after: float = _STALL_AFTER_S,
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
        # sends from inside a poll that a sender may already be in.
        self._send_lock = threading.RLock()
        self._state = threading.Lock()  # guards _entries
        self._reap_lock = threading.Lock()  # one receipt poll at a time
        self._entries: dict[bytes, _Entry] = {}
        self._next_nonce: int | None = None
        self._fees: tuple[int, int, float] | None = None
        self._last_reap = float("-inf")
        # A restart or a second writer can leave this many of our nonces taken.
        self._max_skips = 2 * max_in_flight + 16
        self._last_stall_check = float("-inf")

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
        """Under the send lock: take the next nonce, sign, broadcast, and read
        the node's answer before anyone else may take a nonce.

        web3 never resends a transaction (see `build_http_provider`), so each
        answer is about exactly this broadcast. A refused broadcast is retried
        freely: the node holds nothing of ours. A broadcast that went
        UNANSWERED is different: the node may hold or even have mined it, so
        the action is pinned to that nonce and those signed bytes (re-sending
        them on the same nonce is safe, only one transaction per nonce can
        ever execute). Only the answers listed below settle it; anything else
        leaves it unknowable. The one exception: when every copy failed to
        even connect (`failed_before_connecting`), the node never saw one, so
        the send was refused and its nonce stays free.
        """
        skips = 0
        resynced = fee_refreshed = False
        # Set by an unanswered send: (raw, tx_hash, nonce, that send's error).
        # From then on the same bytes are resent, never re-signed: a fee
        # refreshed meanwhile would give a second copy a new hash, and the
        # node would answer "same nonce" about our own first copy.
        pinned: tuple[bytes, bytes, int, Exception] | None = None
        # True while every delivery of the pinned bytes failed before
        # connecting; one answer or one possible delivery ends it for good.
        never_connected = False
        waited = False  # a pinned resend has waited for our lower nonces
        deadline: float | None = None
        while True:
            if pinned is None:
                if self._next_nonce is None:
                    # "pending" equals the committed nonce on skaled; on
                    # anvil/geth it also counts our own transactions still in
                    # the pool.
                    self._next_nonce = self._rpc.nonce(self.address, "pending")
                nonce = self._next_nonce
                signed = self._sign(base, nonce, gas)
                raw = bytes(signed.raw_transaction)
                tx_hash = bytes(signed.hash)
            else:
                raw, tx_hash, nonce, _ = pinned
            try:
                self._rpc.send_raw(raw)
            except Exception as exc:
                error = exc
            else:
                return self._accept(tx_hash, nonce)
            kind = classify_send_error(error)
            if pinned is not None:
                # The answer to a resend of pinned bytes.
                never_connected = never_connected and failed_before_connecting(error)
                if kind is SendError.DUPLICATE:
                    return self._accept(tx_hash, nonce)
                if (
                    kind is SendError.NONCE_TAKEN
                    and not waited
                    and skips < self._max_skips
                ):
                    # The FIRST resend only. skaled checks its known hashes
                    # before the same-nonce rule and a mined copy would have
                    # been "invalid nonce", so the node holds none of ours and
                    # the action may move on. Relies on the node that holds
                    # our copy being the one answering (SKALE's proxy is
                    # sticky per client IP). After a wait below, not even
                    # that: the wait was reached on a nonce read that
                    # contradicted the node (see there), so the answers are
                    # not from one consistent view.
                    pinned = None
                    skips += 1
                    self._next_nonce = nonce + 1
                    continue
                if kind is SendError.NONCE_INVALID:
                    # skaled checks the nonce before its queue, so a copy that
                    # already MINED is answered like this. Look for our receipt.
                    mined, committed = self._own_receipt(tx_hash)
                    if mined:
                        return self._accept(tx_hash, nonce)
                    with self._state:
                        below = {
                            h
                            for h, e in self._entries.items()
                            if not e.done and e.pending.nonce < nonce
                        }
                    # Not mined, and the nonce is above the committed one while
                    # ours below it are in flight: the one case MTM being off
                    # explains (on an MTM chain it means the nonce read is
                    # stale). Either way the only safe retry is these bytes on
                    # this nonce, once ours below have landed. Serial mode is
                    # NOT switched on: a resend's answer cannot prove MTM off.
                    if (
                        mined is False
                        and committed is not None
                        and committed < nonce
                        and below
                    ):
                        if deadline is None:
                            deadline = self._clock() + self._slot_timeout
                        self._wait_until_done(below, max(0.0, deadline - self._clock()))
                        if not self._any_in_flight(below):
                            waited = True
                            continue
                if never_connected:
                    # No copy ever reached the node (refused or timed-out
                    # connect, unresolved name): refused, not unknowable. The
                    # nonce is not counted, so the next send reuses it and no
                    # gap is left for the stall healer to fill.
                    raise pinned[3]
                # FEE_LOW (checked before the nonce and the queue, so the node
                # may well hold our copy), OTHER, TRANSPORT, a failed lookup,
                # a nonce used by something we cannot find, a nonce read the
                # node's answer contradicts, a same-nonce answer after a wait,
                # or our lower nonces never landing: unknowable. Count the
                # nonce as used and keep watching the FIRST hash (stall
                # healing marks it dropped if it never mines), and raise the
                # first error.
                self._accept(tx_hash, nonce)
                raise pinned[3]
            if kind is SendError.TRANSPORT:
                # No answer. A queued transaction is invisible to a lookup by
                # hash, so send the identical bytes once more: same hash, same
                # nonce, the node takes it at most once.
                pinned = (raw, tx_hash, nonce, error)
                never_connected = failed_before_connecting(error)
                continue
            if kind is SendError.DUPLICATE:
                return self._accept(tx_hash, nonce)
            if kind is SendError.NONCE_TAKEN and skips < self._max_skips:
                # A restart or a second writer left a transaction on this
                # nonce. Ours was refused, so the next nonce is free to try.
                skips += 1
                self._next_nonce = nonce + 1
                continue
            if kind is SendError.NONCE_INVALID:
                # A first answer needs no receipt lookup: the same bytes never
                # reach the node twice outside the pinned resends above, so
                # this refusal is about a transaction that cannot have mined.
                committed = self._rpc.nonce(self.address, "latest")
                if nonce > committed and self._in_flight_count():
                    # The node takes only the very next nonce: Multi-
                    # Transaction Mode is off. One transaction at a time.
                    if self._max_in_flight != 1:
                        log.warning(
                            "node refused admin nonce %d above committed %d: "
                            "Multi-Transaction Mode is off, sending one "
                            "transaction at a time",
                            nonce,
                            committed,
                        )
                        self._max_in_flight = 1
                    self._wait_for_slot()
                    continue
                if not resynced:
                    resynced = True
                    self._next_nonce = committed
                    continue
            if kind is SendError.FEE_LOW and not fee_refreshed:
                fee_refreshed = True
                self._fees = None
                continue
            raise error

    def _own_receipt(self, tx_hash: bytes) -> tuple[bool | None, int | None]:
        """After a resend was answered "invalid nonce": `(mined, committed)`,
        the committed nonce read once and whether OUR transaction has a
        receipt. `(None, None)` if that cannot be told.

        Receipt indexing can trail the committed nonce, and a nonce read and a
        receipt read may be served by different nodes, so the lookup is
        repeated a few times, `poll_interval` apart.
        """
        try:
            committed = self._rpc.nonce(self.address, "latest")
            for attempt in range(_OWN_RECEIPT_LOOKUPS):
                if attempt:
                    self._sleep(self._poll_interval)
                (receipt,) = self._rpc.receipts([tx_hash])
                if receipt is not None:
                    return True, committed
        except Exception as exc:
            log.warning("admin receipt lookup after a refused resend failed: %s", exc)
            return None, None
        return False, committed

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
        filler or another transaction took its nonce."""
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
                            f"{entry.pending.nonce}) was dropped: its nonce went to a "
                            "gap filler or to another transaction, so it never ran"
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
        """Fetch receipts for everything in flight, then heal a stalled nonce."""
        self._reap()
        self._heal_stall()

    def _heal_stall(self) -> None:
        """Unblock the queue when the node has lost our transactions.

        skaled parks every later nonce behind a gap, so one transaction the
        node dropped (it failed re-validation when a block was proposed) or
        never received would stop every admin send, user trades included,
        until a restart. The fix is a 0-value transfer to ourselves on the
        missing nonce. An outage leaves a whole run of them (each send whose
        answers were lost counts its nonce), so one pass fills every gap it
        can prove, not one per `stall_after`: 16 lost nonces would otherwise
        hold every admin transaction for four minutes.
        """
        now = self._clock()
        if now - self._last_stall_check < self._stall_after:
            return
        if not self._send_lock.acquire(blocking=False):
            return  # whoever holds it is polling too
        try:
            with self._state:
                waiting = [
                    e
                    for e in self._entries.values()
                    if not e.done and e.superseded_by is None
                ]
            if not waiting:
                return
            oldest = min(waiting, key=lambda e: e.pending.nonce)
            if now - oldest.sent_at < self._stall_after:
                return
            self._last_stall_check = now
            committed = self._rpc.nonce(self.address, "latest")
            if committed > oldest.pending.nonce:
                self._settle_used_nonce(oldest, now)
                return
            self._fill_gaps(committed, now)
        except Exception as exc:
            log.warning("admin stall check failed: %s", exc)
        finally:
            self._send_lock.release()

    def _fill_gaps(self, committed: int, now: float) -> None:
        """Send a filler on each nonce from `committed` up that is provably a
        gap, and stop at the first one that is not.

        A nonce below our counter is a gap when nothing of ours is in flight
        on it, or what is has waited `stall_after` and is not in the node's
        queue. One read of the queue serves the whole run. That read lists
        only the current queue, so a transaction of ours parked behind a gap
        looks lost: its filler is refused (same nonce) and the run ends there.
        The refusal is the backstop for every wrong "lost": the node never
        lets a filler take a nonce it holds a transaction on.
        """
        queued = self._rpc.pending_hashes()
        with self._state:
            by_nonce: dict[int, list[_Entry]] = {}
            for entry in self._entries.values():
                by_nonce.setdefault(entry.pending.nonce, []).append(entry)
        # A fresh fee, once per pass: a transaction the node dropped for its
        # price would otherwise get an equally doomed filler.
        self._fees = None
        nonce = committed
        while self._next_nonce is not None and nonce < self._next_nonce:
            with self._state:
                here = by_nonce.get(nonce, [])
                if any(e.done for e in here):
                    return  # used after all: the nonce read was behind
                live = [e for e in here if e.superseded_by is None]
                if any(
                    now - e.sent_at < self._stall_after or e.pending.tx_hash in queued
                    for e in live
                ):
                    return  # too young to call lost, or still queued
            filler = self._send_filler(nonce)
            if filler is None:
                return
            with self._state:
                for entry in live:
                    entry.superseded_by = filler.tx_hash
            nonce += 1

    def _settle_used_nonce(self, entry: _Entry, now: float) -> None:
        """The node's committed nonce is past `entry`'s: its nonce was used,
        by it (its receipt is due) or by another transaction, in which case it
        can never run. A receipt that is not readable yet is not proof of the
        second, so it takes two misses `stall_after` apart to call it dropped.
        """
        (receipt,) = self._rpc.receipts([entry.pending.tx_hash])
        with self._state:
            if entry.done:
                return  # a concurrent poll stored its receipt
            if receipt is not None:
                entry.receipt = receipt
                entry.done_at = now
                return
            if entry.suspect_since is None:
                entry.suspect_since = now
                return
            if now - entry.suspect_since < self._stall_after:
                return
            entry.dropped = True
            entry.done_at = now
        log.error(
            "admin tx %s lost nonce %d to another transaction",
            HexBytes(entry.pending.tx_hash).to_0x_hex(),
            entry.pending.nonce,
        )

    def _send_filler(self, nonce: int) -> PendingTx | None:
        base = {"to": self.address, "data": b"", "value": 0}
        signed = self._sign(base, nonce, _TRANSFER_GAS)
        try:
            self._rpc.send_raw(bytes(signed.raw_transaction))
        except Exception as exc:
            if classify_send_error(exc) in (SendError.NONCE_TAKEN, SendError.DUPLICATE):
                # The node holds a transaction on this nonce (ours parked
                # behind a gap, or another writer's): not a gap after all.
                log.info(
                    "admin nonce %d is held by the node, no filler: %s", nonce, exc
                )
            else:
                log.warning("gap filler for admin nonce %d refused: %s", nonce, exc)
            return None
        log.error(
            "admin nonce %d stalled (the node lost the transaction); sent a "
            "0-value filler %s so the queue behind it can move",
            nonce,
            HexBytes(signed.hash).to_0x_hex(),
        )
        pending = PendingTx(tx_hash=bytes(signed.hash), nonce=nonce)
        with self._state:
            self._entries[pending.tx_hash] = _Entry(pending, self._clock())
        # The counter is not touched: a filler only ever takes a nonce below it.
        return pending

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
                # One receipt per hash or none of them: a short answer must
                # never pair a receipt with the wrong entry.
                pairs = list(zip(waiting, receipts, strict=True))
            except Exception as exc:
                log.warning("admin receipt poll failed: %s", exc)
                return
            with self._state:
                for entry, receipt in pairs:
                    if receipt is not None:
                        entry.receipt = receipt
                        entry.done_at = now
                # Until nothing changes: a lost filler can itself have been
                # superseded by a later one (original -> filler -> filler).
                changed = True
                while changed:
                    changed = False
                    for entry in waiting:
                        if entry.done or entry.superseded_by is None:
                            continue
                        filler = self._entries.get(entry.superseded_by)
                        if filler is not None and (
                            filler.receipt is not None or filler.dropped
                        ):
                            # Its nonce went to the filler (or past it), so it
                            # can never run.
                            entry.dropped = True
                            entry.done_at = now
                            changed = True
        finally:
            self._reap_lock.release()
