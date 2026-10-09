"""Pipelined, nonce-managed transaction sender for the admin key.

SKALE runs our chain in Multi-Transaction Mode: one account may have many
transactions in one block, provided the client hands them over without
waiting for each receipt. The old `send_admin_tx` held one lock from the nonce
read to the receipt, so the admin key landed exactly one transaction per block
(~4.6 s each) and the market sync, user settlement (`matchOrders`), faucet and
users' gas top-ups all queued behind one another.

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
- The fee billed is `maxFeePerGas` in full, so every transaction is priced at
  the current `eth_gasPrice` with no headroom (`current_fee_params`). The
  price is checked before the nonce and the queue: a transaction under it is
  refused, and a queued one is dropped once the price rises past it.

Nonce, sign and broadcast happen under the send lock, normally one round trip;
the retries of a refused send, the resend after a lost answer and healing a
stalled nonce run under it too. Receipts are polled outside it, up to 100
hashes per JSON-RPC batch. `submit_many` broadcasts up to 100 transactions in
one JSON-RPC batch of `eth_sendRawTransaction` (live on SKALE: 100 in 431 ms,
all in one block); skaled imports a batch's items one after another and
answers each on its own.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from eth_account.signers.local import LocalAccount
from hexbytes import HexBytes
from web3 import Web3
from web3.contract.contract import ContractFunction
from web3.exceptions import TimeExhausted
from web3.types import TxReceipt

from agentpit.domain.exceptions import AdminGasPausedError
from agentpit.onchain.chain_rpc import (
    BatchUnanswered,
    ChainRpc,
    SendError,
    classify_send_error,
    failed_before_connecting,
)

log = logging.getLogger(__name__)

# Transactions per broadcast batch. `submit_many` also keeps a batch within
# `max_in_flight`, since every one of them takes a slot.
_SEND_BATCH = 100
# A plain native-token transfer. Public: `UserGasSponsor` books each gas
# top-up it sends at this figure.
TRANSFER_GAS = 21_000
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
# Batch answers that say the node refused the item before importing it. Only
# an item refused like this is sent again (on another nonce); any other
# answer is that item's result.
_RESEND_AFTER = frozenset(
    {
        SendError.FEE_LOW,
        SendError.NONCE_TAKEN,
        SendError.NONCE_INVALID,
        SendError.QUEUE_FULL,
        SendError.BALANCE_LOW,
    }
)


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
        "value",
        "receipt",
        "superseded_by",
        "dropped",
        "done_at",
        "suspect_since",
    )

    def __init__(self, pending: PendingTx, sent_at: float, value: int = 0):
        self.pending = pending
        self.sent_at = sent_at
        # The native value the transaction sends (a gas top-up is all value);
        # the breaker's cached balance loses it along with the gas.
        self.value = value
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
    `submit` now and `wait_all` later. The sync hands a whole chunk of
    markets to `submit_many`, one JSON-RPC batch, so it lands in one or two
    blocks after one round trip.

    The admin key pays for users' fills and gas top-ups, so the sender also keeps a
    breaker on its balance (`alarm_gas`, `stop_gas`): below `stop_gas` every
    send but an `essential` one is refused before anything is broadcast.
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
        alarm_gas: int = 0,
        stop_gas: int = 0,
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
        # Guards _entries. Lock order: _state, then _gas_lock; never take _state
        # while holding _gas_lock.
        self._state = threading.Lock()
        self._reap_lock = threading.Lock()  # one receipt poll at a time
        self._entries: dict[bytes, _Entry] = {}
        self._next_nonce: int | None = None
        self._fees: tuple[int, int, float] | None = None
        # Set while a batch refused as a whole goes out one at a time: the
        # batch's own fees, so each single send signs the batch's very bytes.
        # Only `_sign_and_send` reads it; a fee refusal clears it.
        self._pinned_fees: tuple[int, int] | None = None
        self._last_reap = float("-inf")
        # A restart or a second writer can leave this many of our nonces taken.
        self._max_skips = 2 * max_in_flight + 16
        self._last_stall_check = float("-inf")
        # The breaker's floors, in gas valued at the current price (0 = off).
        self._alarm_gas = alarm_gas
        self._stop_gas = stop_gas
        self._gas_lock = threading.Lock()  # guards _admin_balance
        # Unknown until the first refresh, and unknown means allowed: a node we
        # cannot read must not stop trading.
        self._admin_balance: int | None = None

    @property
    def address(self) -> str:
        return self._account.address

    @property
    def max_in_flight(self) -> int:
        return self._max_in_flight

    # --- admin-gas breaker ------------------------------------------

    def refresh_gas_balance(self) -> int:
        """Read the admin balance from the node and make it the breaker's figure."""
        balance = self._rpc.balance(self.address)
        with self._gas_lock:
            self._admin_balance = balance
        return balance

    def gas_state(self) -> str:
        """'unknown' before the first refresh; then 'paused' below `stop_gas`
        worth of gas at the current price, 'low' below `alarm_gas`, else 'ok'."""
        with self._gas_lock:
            balance = self._admin_balance
        if balance is None:
            return "unknown"
        price = self._fee_params()[0]
        if self._stop_gas and balance < self._stop_gas * price:
            return "paused"
        if self._alarm_gas and balance < self._alarm_gas * price:
            return "low"
        return "ok"

    def check_sponsored(self) -> None:
        """Raise `AdminGasPausedError` while sponsored sends are refused."""
        if self._stop_gas and self.gas_state() == "paused":
            raise AdminGasPausedError()

    def _gate(self, essential: bool) -> None:
        # Sponsored by default, so a new admin-paid feature is covered without
        # anyone remembering to opt it in. Only the oracle, the catalogue sync
        # and the settlement of an already-admitted placement pass essential=True.
        if not essential:
            self.check_sponsored()

    def _debit(self, receipt, value: int = 0) -> None:
        """Take a mined send's cost off the cached balance, so a burst between
        two refreshes still trips the breaker: its gas, plus the `value` it
        sent unless it reverted (a gas top-up is all value). Fillers and
        receipts nobody waits for are not debited; the next refresh corrects
        for them."""
        used = receipt.get("gasUsed")
        price = receipt.get("effectiveGasPrice")
        if used is None or price is None:
            return
        cost = int(used) * int(price)
        if receipt.get("status") == 1:
            cost += value
        with self._gas_lock:
            if self._admin_balance is not None:
                self._admin_balance -= cost

    # --- sending ----------------------------------------------------

    def submit(
        self,
        fn: ContractFunction,
        *,
        gas: int | None = None,
        gas_buffer_pct: int = 20,
        essential: bool = False,
    ) -> PendingTx:
        """Broadcast `fn(...)` and return without waiting for it to mine.

        `gas` is a static limit; without one the node's estimate plus
        `gas_buffer_pct` is used. An estimate runs on committed state, so a
        transaction that depends on one of ours still in flight should pass
        a static limit.

        Every public send takes `essential`: only an essential one goes out
        while the admin-gas breaker is paused (`AdminGasPausedError` otherwise).
        """
        self._gate(essential)
        return self._submit(self._call_base(fn), gas, gas_buffer_pct)

    def submit_value(
        self,
        to: str,
        value_wei: int,
        *,
        essential: bool = False,
        slot_timeout: float | None = None,
    ) -> PendingTx:
        """Broadcast a plain native-token transfer. `slot_timeout` replaces the
        sender's own wait for a free slot (send lock included) for this send:
        a caller holding a lock meanwhile passes a shorter one."""
        self._gate(essential)
        return self._submit(
            _value_base(to, value_wei), TRANSFER_GAS, 0, slot_timeout=slot_timeout
        )

    def submit_many(
        self, calls: list[tuple[ContractFunction, int]], *, essential: bool = False
    ) -> list[PendingTx | Exception]:
        """Broadcast many `(fn, static gas limit)` calls in JSON-RPC batches on
        consecutive nonces and return without waiting for any to mine.

        One result per call, in order: its `PendingTx`, or the exception a
        `submit` of it would have raised. Nothing is estimated: a batch is
        signed in one go. A call the node refuses is sent again on a later
        nonce, so callers must not rely on the calls running in the order
        given. `_BatchSend` says how each broadcast is settled.

        A paused breaker refuses the whole call (`AdminGasPausedError`), not
        item by item.
        """
        self._gate(essential)
        items: list[tuple[dict, int] | Exception] = []
        for fn, gas in calls:
            try:
                if not isinstance(gas, int) or gas <= 0:
                    raise ValueError(
                        f"submit_many needs a static gas limit, got {gas!r}"
                    )
                items.append((self._call_base(fn), gas))
            except Exception as exc:  # a call that cannot be encoded fails alone
                items.append(exc)
        return self._submit_many(items)

    def submit_values(
        self, transfers: list[tuple[str, int]], *, essential: bool = False
    ) -> list[PendingTx | Exception]:
        """`submit_many` for plain native-token transfers `(to, value_wei)`."""
        self._gate(essential)
        items: list[tuple[dict, int] | Exception] = []
        for to, value_wei in transfers:
            try:
                items.append((_value_base(to, value_wei), TRANSFER_GAS))
            except Exception as exc:
                items.append(exc)
        return self._submit_many(items)

    def _call_base(self, fn: ContractFunction) -> dict:
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
        return {
            "to": built["to"],
            "data": built["data"],
            "value": built.get("value", 0),
        }

    def send(
        self,
        fn: ContractFunction,
        *,
        timeout: float,
        gas: int | None = None,
        gas_buffer_pct: int = 20,
        essential: bool = False,
    ) -> TxReceipt:
        """`submit` then `wait`: the receipt whatever its status."""
        return self.wait(
            self.submit(
                fn, gas=gas, gas_buffer_pct=gas_buffer_pct, essential=essential
            ),
            timeout=timeout,
        )

    def send_value(
        self,
        to: str,
        value_wei: int,
        *,
        timeout: float,
        essential: bool = False,
        slot_timeout: float | None = None,
    ) -> TxReceipt:
        """`submit_value` then `wait`: the receipt whatever its status. With a
        `slot_timeout`, the slot wait is capped at it and counts against
        `timeout`, which then bounds the whole call."""
        started = self._clock()
        pending = self.submit_value(
            to, value_wei, essential=essential, slot_timeout=slot_timeout
        )
        if slot_timeout is not None:
            timeout = max(0.0, timeout - (self._clock() - started))
        return self.wait(pending, timeout=timeout)

    def _submit(
        self,
        base: dict,
        gas: int | None,
        gas_buffer_pct: int,
        *,
        slot_timeout: float | None = None,
    ) -> PendingTx:
        if gas is None:
            gas = self._estimate(base) * (100 + gas_buffer_pct) // 100
        limit = self._slot_timeout if slot_timeout is None else slot_timeout
        # One deadline for the whole wait, set before queueing on the send
        # lock: submitters queued behind each other share it instead of each
        # starting its own once it finally holds the lock.
        deadline = self._clock() + limit
        if not self._send_lock.acquire(timeout=max(0.0, deadline - self._clock())):
            raise TimeExhausted(
                f"no free admin transaction slot after {limit:g}s "
                "(queued behind other submitters)"
            )
        try:
            self._wait_for_slot(deadline, limit=limit)
            return self._sign_and_send(base, gas)
        finally:
            self._send_lock.release()

    def _submit_many(
        self, items: list[tuple[dict, int] | Exception]
    ) -> list[PendingTx | Exception]:
        results: dict[int, PendingTx | Exception] = {
            i: item for i, item in enumerate(items) if isinstance(item, Exception)
        }
        todo = [i for i, item in enumerate(items) if not isinstance(item, Exception)]
        stop: Exception | None = None
        while todo:
            if stop is not None:
                # The node is out of reach or no slot frees up: more batches
                # would only add sends whose answers are lost (each a nonce
                # for the stall healer to fill) or wait out the same timeout.
                results.update((i, stop) for i in todo)
                break
            batch = [items[i] for i in todo[:_SEND_BATCH]]
            done, stop = self._send_round(batch)  # type: ignore[arg-type]
            results.update(zip(todo, done))
            todo = todo[len(done) :]
        return [results[i] for i in range(len(items))]

    def _send_round(
        self, items: list[tuple[dict, int]]
    ) -> tuple[list[PendingTx | Exception], Exception | None]:
        """One hold of the send lock: send as many of `items`, from the front,
        as one batch may hold. Returns their results, and the error that
        should stop the sending if there was one. The lock is released between
        batches, so user trades are not held up by a long sync."""
        deadline = self._clock() + self._slot_timeout
        if not self._send_lock.acquire(timeout=max(0.0, deadline - self._clock())):
            exc = TimeExhausted(
                f"no free admin transaction slot after {self._slot_timeout:g}s "
                "(queued behind other submitters)"
            )
            return [exc], exc
        try:
            # Read under the lock: a send that found Multi-Transaction Mode
            # off may have set the limit to 1 while this one queued.
            size = min(len(items), self._max_in_flight)
            if size == 1:
                result = self._send_one(items[0], deadline)
                return [result], _stops_sending(result)
            return _BatchSend(self, items[:size], deadline).run()
        finally:
            self._send_lock.release()

    def _send_one(
        self, item: tuple[dict, int], deadline: float
    ) -> PendingTx | Exception:
        """`_submit` of one item, with the send lock already held."""
        base, gas = item
        try:
            self._wait_for_slot(deadline)
            return self._sign_and_send(base, gas)
        except Exception as exc:
            return exc

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
        value = int(base.get("value") or 0)  # kept on the entry for the breaker
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
                signed = self._sign(base, nonce, gas, self._pinned_fees)
                raw = bytes(signed.raw_transaction)
                tx_hash = bytes(signed.hash)
            else:
                raw, tx_hash, nonce, _ = pinned
            try:
                self._rpc.send_raw(raw)
            except Exception as exc:
                error = exc
            else:
                return self._accept(tx_hash, nonce, value)
            kind = classify_send_error(error)
            if pinned is not None:
                # The answer to a resend of pinned bytes.
                never_connected = never_connected and failed_before_connecting(error)
                if kind is SendError.DUPLICATE:
                    return self._accept(tx_hash, nonce, value)
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
                        return self._accept(tx_hash, nonce, value)
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
                self._accept(tx_hash, nonce, value)
                raise pinned[3]
            if kind is SendError.TRANSPORT:
                # No answer. A queued transaction is invisible to a lookup by
                # hash, so send the identical bytes once more: same hash, same
                # nonce, the node takes it at most once.
                pinned = (raw, tx_hash, nonce, error)
                never_connected = failed_before_connecting(error)
                continue
            if kind is SendError.DUPLICATE:
                return self._accept(tx_hash, nonce, value)
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
                self._pinned_fees = None  # the batch's fee is too low now
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
            mined = tx_hash in self._find_receipts([tx_hash])
        except Exception as exc:
            log.warning("admin receipt lookup after a refused resend failed: %s", exc)
            return None, None
        return mined, committed

    def _find_receipts(self, tx_hashes: list[bytes]) -> set[bytes]:
        """Which of `tx_hashes` have a receipt. One that is not found is asked
        for again, up to `_OWN_RECEIPT_LOOKUPS` times `poll_interval` apart:
        receipt indexing can trail the committed nonce. Raises if a lookup
        fails."""
        found: set[bytes] = set()
        for attempt in range(_OWN_RECEIPT_LOOKUPS):
            missing = [h for h in tx_hashes if h not in found]
            if not missing:
                break
            if attempt:
                self._sleep(self._poll_interval)
            for tx_hash, receipt in zip(
                missing, self._rpc.receipts(missing), strict=True
            ):
                if receipt is not None:
                    found.add(tx_hash)
        return found

    def _sign(
        self, base: dict, nonce: int, gas: int, fees: tuple[int, int] | None = None
    ):
        """Sign at `fees` (maxFeePerGas, maxPriorityFeePerGas), or at the
        current ones. Signatures are deterministic (RFC 6979): the same
        fields give the same bytes."""
        max_fee, priority = fees if fees is not None else self._fee_params()
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

    def _accept(self, tx_hash: bytes, nonce: int, value: int = 0) -> PendingTx:
        pending = PendingTx(tx_hash=tx_hash, nonce=nonce)
        with self._state:
            self._entries[tx_hash] = _Entry(pending, self._clock(), value)
        # Never back: a batch may settle an early nonce after later ones (a
        # receipt lookup comes last), and every nonce up to the highest one
        # accepted is used. The single path moves the counter back on its
        # own when it must (a resync), not through here.
        if self._next_nonce is None or self._next_nonce <= nonce:
            self._next_nonce = nonce + 1
        return pending

    def _fee_params(self) -> tuple[int, int]:
        now = self._clock()
        # One read of the cache: `gas_state` calls this without the send lock,
        # and a send under it may clear `_fees` at any point.
        fees = self._fees
        if fees is None or now - fees[2] > self._fee_ttl:
            max_fee, priority = self._rpc.fee_params()
            fees = self._fees = (max_fee, priority, now)
        return fees[0], fees[1]

    def _wait_for_slot(
        self,
        deadline: float | None = None,
        need: int = 1,
        *,
        limit: float | None = None,
    ) -> None:
        """Poll until `need` slots are free (a batch takes one per item).
        `limit` is the bound `deadline` was set from, for the error message."""
        if limit is None:
            limit = self._slot_timeout
        if deadline is None:
            deadline = self._clock() + limit
        while self._in_flight_count() + need > self._max_in_flight:
            if self._clock() >= deadline:
                raise TimeExhausted(
                    f"no {need} free admin transaction slot(s) after "
                    f"{limit:g}s ({self._max_in_flight} in flight)"
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
                        # _state, then _gas_lock
                        self._debit(entry.receipt, entry.value)
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
        gap, up to the first one that is not.

        A nonce below our counter is a gap when nothing of ours is in flight
        on it, or what is has waited `stall_after` and is not in the node's
        queue. One read of the queue serves the whole run, and the run's
        fillers go out in one JSON-RPC batch (`_SEND_BATCH` at most per
        batch). That read lists only the current queue, so a transaction of
        ours parked behind a gap looks lost: its filler is refused (same
        nonce). The refusal is the backstop for every wrong "lost": the node
        never lets a filler take a nonce it holds a transaction on. Each
        filler is judged by its own answer, and a refused one ends the run:
        no later batch of fillers goes out in this pass.
        """
        queued = self._rpc.pending_hashes()
        with self._state:
            by_nonce: dict[int, list[_Entry]] = {}
            for entry in self._entries.values():
                by_nonce.setdefault(entry.pending.nonce, []).append(entry)
        # A fresh fee, once per pass: a transaction the node dropped for its
        # price would otherwise get an equally doomed filler.
        self._fees = None
        run: list[tuple[int, list[_Entry]]] = []  # (gap nonce, ours on it)
        nonce = committed
        while self._next_nonce is not None and nonce < self._next_nonce:
            with self._state:
                here = by_nonce.get(nonce, [])
                if any(e.done for e in here):
                    break  # used after all: the nonce read was behind
                live = [e for e in here if e.superseded_by is None]
                if any(
                    now - e.sent_at < self._stall_after or e.pending.tx_hash in queued
                    for e in live
                ):
                    break  # too young to call lost, or still queued
            run.append((nonce, live))
            nonce += 1
        for start in range(0, len(run), _SEND_BATCH):
            part = run[start : start + _SEND_BATCH]
            fillers = self._send_fillers([n for n, _ in part])
            with self._state:
                for (_, live), filler in zip(part, fillers, strict=True):
                    if filler is not None:
                        for entry in live:
                            entry.superseded_by = filler.tx_hash
            if any(filler is None for filler in fillers):
                return

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

    def _send_filler(
        self,
        nonce: int,
        why: str = "stalled (the node lost the transaction)",
        level: int = logging.ERROR,
    ) -> PendingTx | None:
        signed = self._sign(_filler_base(self.address), nonce, TRANSFER_GAS)
        try:
            self._rpc.send_raw(bytes(signed.raw_transaction))
        except Exception as exc:
            _log_refused_filler(nonce, exc)
            return None
        log.log(
            level,
            "admin nonce %d %s; sent a 0-value filler %s so the queue behind "
            "it can move",
            nonce,
            why,
            HexBytes(signed.hash).to_0x_hex(),
        )
        return self._track_filler(bytes(signed.hash), nonce)

    def _send_fillers(self, nonces: list[int]) -> list[PendingTx | None]:
        """`_send_filler` for each of `nonces`, in one JSON-RPC batch when
        there is more than one: one result per nonce, None where the node
        refused the filler or its answer was lost."""
        if len(nonces) == 1:
            return [self._send_filler(nonces[0])]
        fees = self._fee_params()
        base = _filler_base(self.address)
        signed = [self._sign(base, n, TRANSFER_GAS, fees) for n in nonces]
        try:
            answers = self._rpc.send_raw_batch(
                [bytes(s.raw_transaction) for s in signed]
            )
            if len(answers) != len(nonces):
                raise BatchUnanswered(f"{len(answers)} answers for {len(nonces)}")
        except Exception as exc:
            # A filler whose answer is lost is not tracked, as on the single
            # path: if the node took it, it mines and the healer settles our
            # transaction on that nonce as used by another.
            log.warning(
                "gap fillers for admin nonces %d-%d failed: %s",
                nonces[0],
                nonces[-1],
                exc,
            )
            return [None] * len(nonces)
        out: list[PendingTx | None] = []
        for nonce, tx, answer in zip(nonces, signed, answers, strict=True):
            if answer is not None:
                _log_refused_filler(nonce, answer)
                out.append(None)
            else:
                out.append(self._track_filler(bytes(tx.hash), nonce))
        sent = [n for n, f in zip(nonces, out) if f is not None]
        if sent:
            log.error(
                "admin nonces %s stalled (the node lost the transactions); sent "
                "0-value fillers so the queue behind them can move",
                sent,
            )
        return out

    def _track_filler(self, tx_hash: bytes, nonce: int) -> PendingTx:
        pending = PendingTx(tx_hash=tx_hash, nonce=nonce)
        with self._state:
            self._entries[tx_hash] = _Entry(pending, self._clock())
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


def _value_base(to: str, value_wei: int) -> dict:
    return {"to": Web3.to_checksum_address(to), "data": b"", "value": value_wei}


def _filler_base(address: str) -> dict:
    return {"to": address, "data": b"", "value": 0}


def _log_refused_filler(nonce: int, exc: Exception) -> None:
    if classify_send_error(exc) in (SendError.NONCE_TAKEN, SendError.DUPLICATE):
        # The node holds a transaction on this nonce (ours parked behind a
        # gap, or another writer's): not a gap after all.
        log.info("admin nonce %d is held by the node, no filler: %s", nonce, exc)
    else:
        log.warning("gap filler for admin nonce %d refused: %s", nonce, exc)


def stops_sending(exc: BaseException) -> bool:
    """Does this error from a submit say the node is out of reach or no admin
    slot frees up? Then more sends would only add nonces whose answers are
    lost (each one for the stall healer to fill) or wait out the same timeout.

    Out of reach: no answer (TRANSPORT), a connect that failed included
    (`failed_before_connecting` errors are TRANSPORT too). No slot: the
    `TimeExhausted` of a slot wait; a submit never waits for a receipt, so
    its `TimeExhausted` is always that one.
    """
    return (
        isinstance(exc, TimeExhausted)
        or classify_send_error(exc) is SendError.TRANSPORT
    )


def _stops_sending(result: PendingTx | Exception) -> Exception | None:
    """The error, when `result` is one that `stops_sending`: the rest of a
    `submit_many` is then not sent."""
    if isinstance(result, Exception) and stops_sending(result):
        return result
    return None


class _BatchSend:
    """One JSON-RPC batch of admin transactions on consecutive nonces, from
    signing to the settled outcome of each item. Runs under the send lock.

    What the node answered each item decides what happens to it:
    - OK or "already known": accepted.
    - A refusal. This was the first delivery of that signature and the node
      answered it, so the node holds nothing of ours on that nonce. If a later
      nonce of the batch is ours, a 0-value filler takes this one (a gap would
      park every later nonce); if none is, the nonce is simply free again, as
      after a refused single send. A refusal that says the node never
      imported the item (`_RESEND_AFTER`: fee, nonce, full queue, balance)
      then goes out once more through the single path, on a fresh nonce:
      safe, because the node provably never took its first signature. Any
      other answer proves nothing about that, so it is the item's result.
      Once one such re-send is refused as well, the refusal is about the
      node or the key, not the item: the rest are not sent again.
    - No answer (the whole batch, or one item's timeout). The node may hold
      it, so the identical bytes go out once more (after a short pause when
      the whole batch was lost) and only the single path's resend rules
      settle it: OK or "already known" = accepted; "invalid nonce" with our
      receipt found = accepted (it mined); anything else = unknowable: the
      nonce is counted, the hash tracked for the stall healer, and the
      result is the FIRST error. Its action is never signed again. When
      every delivery of the whole batch failed before connecting, nothing
      left the host: no nonce is used and every item gets the first error.
    - One error for the whole batch (skaled, above 128 requests): no item was
      looked at, so they go out one at a time from the same first nonce,
      signed at the batch's fees so that each copy is the batch's very bytes.
      Once one of them is refused, the rest are not sent.
    """

    def __init__(
        self, sender: AdminTxSender, items: list[tuple[dict, int]], deadline: float
    ):
        self._s = sender
        self._items = items
        self._deadline = deadline
        self._first = 0
        self._raws: list[bytes] = []
        self._hashes: list[bytes] = []
        self._results: list[PendingTx | Exception | None] = [None] * len(items)
        self._counted = [False] * len(items)  # the nonce is used by our tx
        self._stop: Exception | None = None
        self._fees: tuple[int, int] | None = None  # every item is signed at these

    def run(self) -> tuple[list[PendingTx | Exception], Exception | None]:
        s = self._s
        n = len(self._items)
        try:
            s._wait_for_slot(self._deadline, need=n)
            if s._next_nonce is None:
                s._next_nonce = s._rpc.nonce(s.address, "pending")
            self._first = s._next_nonce
            # Read afresh: with no headroom over the price, a cached one the
            # node has since raised gets every item refused, and each then
            # goes out again on its own. One read per batch.
            s._fees = None
            self._fees = s._fee_params()
            signed = [
                s._sign(base, self._first + i, gas, self._fees)
                for i, (base, gas) in enumerate(self._items)
            ]
        except Exception as exc:
            return [exc] * n, exc  # nothing was broadcast, no nonce used
        self._raws = [bytes(t.raw_transaction) for t in signed]
        self._hashes = [bytes(t.hash) for t in signed]
        whole: Exception | None = None
        try:
            answers = s._rpc.send_raw_batch(self._raws)
            if len(answers) != n:
                raise BatchUnanswered(f"{len(answers)} answers for {n} sends")
        except Exception as exc:
            if classify_send_error(exc) is not SendError.TRANSPORT:
                return self._one_at_a_time(exc)
            whole = exc
            answers = [exc] * n
        log.debug(
            "admin batch of %d sent on nonces %d-%d",
            n,
            self._first,
            self._first + n - 1,
        )
        refused: list[int] = []
        unanswered: list[int] = []
        for i, answer in enumerate(answers):
            kind = None if answer is None else classify_send_error(answer)
            if kind is None or kind is SendError.DUPLICATE:
                self._take(i)
            elif kind is SendError.TRANSPORT:
                unanswered.append(i)
            else:
                refused.append(i)
        if unanswered:
            self._resend(unanswered, answers, whole)
        self._settle_refused(refused, answers)
        return self._results, self._stop  # type: ignore[return-value]

    def _value(self, i: int) -> int:
        """The native value item `i` sends."""
        return int(self._items[i][0].get("value") or 0)

    def _take(self, i: int) -> None:
        self._counted[i] = True
        self._results[i] = self._s._accept(
            self._hashes[i], self._first + i, self._value(i)
        )

    def _unknowable(self, i: int, first_error: Exception) -> None:
        # Count the nonce and track the hash: it mines, or the stall healer
        # fills its nonce and reports it dropped.
        self._counted[i] = True
        self._s._accept(self._hashes[i], self._first + i, self._value(i))
        self._results[i] = first_error
        self._stop = self._stop or first_error

    def _resend(self, indexes: list[int], first: list, whole: Exception | None) -> None:
        """The node may hold these: the identical bytes once more, each item
        then settled by the single path's resend rules."""
        s = self._s
        if whole is not None:
            # The whole batch went unanswered: give a blip a moment to pass
            # before the identical batch goes out again.
            s._sleep(s._poll_interval * 2)
        try:
            again = s._rpc.send_raw_batch([self._raws[i] for i in indexes])
            if len(again) != len(indexes):
                raise BatchUnanswered(f"{len(again)} answers for {len(indexes)}")
        except Exception as exc:
            if (
                whole is not None
                and failed_before_connecting(whole)
                and failed_before_connecting(exc)
            ):
                # Neither copy reached the node: refused, the nonces stay free.
                log.warning("admin batch never reached the node: %s", whole)
                for i in indexes:
                    self._results[i] = first[i]
                self._stop = self._stop or whole
                return
            # Whatever the resend's failure says, it is not about the first
            # copies: every one of them is unknowable.
            lookups: list[int] = []
            for i in indexes:
                self._unknowable(i, first[i])
        else:
            lookups = []
            for i, answer in zip(indexes, again):
                kind = None if answer is None else classify_send_error(answer)
                if kind is None or kind is SendError.DUPLICATE:
                    self._take(i)
                elif kind is SendError.NONCE_INVALID:
                    # skaled checks the nonce before its queue: a copy that
                    # already MINED is answered like this. Look for our receipt.
                    lookups.append(i)
                else:
                    # FEE_LOW and "same nonce" included: skaled checks the fee
                    # before its queue, and the node answering may not be the
                    # one holding our copy.
                    self._unknowable(i, first[i])
        if lookups:
            try:
                mined = s._find_receipts([self._hashes[i] for i in lookups])
            except Exception as exc:
                log.warning("admin receipt lookup after a batch resend failed: %s", exc)
                mined = set()
            for i in lookups:
                if self._hashes[i] in mined:
                    self._take(i)
                else:
                    self._unknowable(i, first[i])
        lost = [self._first + i for i in indexes if self._results[i] is first[i]]
        if lost:
            log.warning(
                "admin batch: %d send(s) unanswered twice, outcome unknown "
                "(nonces %s counted, the stall healer settles them): %s",
                len(lost),
                lost,
                first[indexes[0]],
            )

    def _settle_refused(self, refused: list[int], first: list) -> None:
        if not refused:
            return
        s = self._s
        # `_accept` left the counter just past our last nonce of the batch:
        # every nonce up to it is ours, unknowable or filled below; the
        # refused ones above it are free again.
        last = max((i for i, c in enumerate(self._counted) if c), default=-1)
        gaps = [i for i in refused if i < last]
        again = {i for i in refused if classify_send_error(first[i]) in _RESEND_AFTER}
        log.warning(
            "admin batch: node refused %d of %d sends (first: %s); %d gap "
            "filler(s), %d action(s) go out again one by one",
            len(refused),
            len(self._items),
            first[refused[0]],
            len(gaps),
            len(again),
        )
        # A fresh fee for the fillers and the resends: a fee refusal would
        # otherwise meet an equally doomed copy.
        s._fees = None
        try:
            for i in gaps:
                # A refused filler is fine: another transaction holds the
                # nonce, it is below the committed one, or MTM is off.
                s._send_filler(
                    self._first + i,
                    why="was refused inside a batch whose later nonces went through",
                    level=logging.WARNING,
                )
        except Exception as exc:
            log.warning(
                "admin batch gap fillers failed, the stall healer will: %s", exc
            )
        refused_again: Exception | None = None
        for i in refused:
            if i not in again or self._stop is not None or refused_again is not None:
                self._results[i] = first[i]  # its own refusal: never sent again
                continue
            result = s._send_one(self._items[i], self._deadline)
            self._results[i] = result
            self._stop = _stops_sending(result)
            if isinstance(result, Exception) and self._stop is None:
                # Refused twice: a full queue, a dry key, a rate limit. One
                # more round trip per item would meet the same answer.
                refused_again = result
        if refused_again is not None:
            log.warning(
                "admin batch: a re-sent action was refused too (%s); the other "
                "refused actions are not sent again",
                refused_again,
            )

    def _one_at_a_time(
        self, refusal: Exception
    ) -> tuple[list[PendingTx | Exception], Exception | None]:
        log.warning(
            "node refused a batch of %d admin transactions as a whole (%s); "
            "sending them one at a time",
            len(self._items),
            refusal,
        )
        s = self._s
        # Re-signed at the batch's own fees, an item on its batch nonce is the
        # batch's very bytes (signatures are deterministic): were the batch
        # taken after all, the node answers "already known" rather than
        # meeting a second copy of the action on a later nonce. That holds
        # only while the price has not risen past the batch's: its fee is the
        # price read for it, with no headroom, and the node checks the price
        # before its queue. A fee refusal clears the pin (`_sign_and_send`)
        # and leaves this rule's trust alone; the window is the one round
        # trip since the batch read its price.
        s._pinned_fees = self._fees
        refused_too: Exception | None = None
        try:
            for i, item in enumerate(self._items):
                if self._stop is not None:
                    self._results[i] = self._stop
                    continue
                if refused_too is not None:
                    self._results[i] = refusal  # not sent
                    continue
                result = s._send_one(item, self._deadline)
                self._results[i] = result
                self._stop = _stops_sending(result)
                if isinstance(result, Exception) and self._stop is None:
                    refused_too = result
        finally:
            s._pinned_fees = None
        if refused_too is not None:
            log.warning(
                "the first single send was refused too (%s); the rest of the "
                "batch is not sent",
                refused_too,
            )
        return self._results, self._stop  # type: ignore[return-value]
