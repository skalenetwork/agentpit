"""Paying the gas of transactions that a user's own key signs.

Wallets are custodial, and SKALE has no protocol-level sponsorship (skaled
refuses EIP-7702, there is no ERC-4337 EntryPoint, and `redeemPositions` pays
only `msg.sender`), so the native coin must be in the user's wallet before the
user's transaction goes out. `UserGasSponsor.send` sizes what the calls need,
has the admin send exactly the shortfall and waits for it to mine (skaled
checks the balance at import, against committed state), then sends the calls
signed by the user. A wallet never holds more than one action's need.

ONE WORKER ONLY. The per-user locks are module-level `threading.Lock`s, which
serialise one account's transactions inside this process: the whole API today
(deploy/Dockerfile.api runs a single uvicorn worker). With several API
processes on one database they would have to become a Postgres advisory lock
(`pg_try_advisory_lock` on the address).
"""

import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Literal

from web3.contract.contract import ContractFunction
from web3.exceptions import TimeExhausted
from web3.types import TxReceipt

from agentpit.config import Settings
from agentpit.datastructures.user import User
from agentpit.db.session import DbSession
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import (
    DomainError,
    GasBudgetExceededError,
    GasPriceMovedError,
    GasTopUpTimeoutError,
    InsufficientGasError,
    TransactionInProgressError,
    TransactionRevertedError,
)
from agentpit.onchain.admin import OnchainAdmin
from agentpit.onchain.chain_rpc import (
    SendError,
    cannot_mine,
    classify_send_error,
    is_balance_low,
)
from agentpit.onchain.tx_sender import TRANSFER_GAS, TxDropped, TxUnknown
from agentpit.onchain.user_wallet import GAS_BUFFER_PCT

log = logging.getLogger(__name__)

SponsorKind = Literal["claim", "split", "merge", "onboarding"]
# Called with (the call's index in `calls`, its transaction's hash) each time a
# call is signed, just before that transaction is broadcast.
OnSigned = Callable[[int, str], None]

_SECONDS_PER_DAY = 86_400  # the sponsored-gas budget resets at 00:00 UTC
# The kinds the daily budget can refuse. Claims and onboarding are booked to
# the same row but never refused for it.
_BUDGETED = frozenset({"split", "merge"})
# 402 for a wallet that could not pay: the kill switch is off, or the node
# still said "balance too low" after the one re-top-up.
_CANNOT_PAY = "the wallet could not pay for this transaction's gas — try again later"

# One lock per lowercased address, shared by every sponsor in the process and
# created under `_locks_guard`. Never pruned: a lock dropped while held would
# let a second holder in.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(address: str) -> threading.Lock:
    key = address.lower()
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.Lock()
        return lock


def _call_hook(
    on_signed: OnSigned | None, index: int
) -> Callable[[str], None] | None:
    """`send_user_tx`'s one-argument hook for call `index`."""
    if on_signed is None:
        return None
    return lambda tx_hash: on_signed(index, tx_hash)


class _Paid:
    """What one sponsored send has had the admin pay for so far, for `_book`:
    the top-ups that mined and the calls' receipts.

    `unseen` is set while a top-up or user transaction is out with no answer
    yet (a receipt, or a refusal that says it can never run). Whatever stops
    the send while it is set, a `BaseException` included, may leave something
    that still mines, so the booking keeps the reservation."""

    def __init__(self) -> None:
        self.topups = 0
        self.receipts: list[TxReceipt] = []
        self.unseen = False

    @property
    def gas(self) -> int:
        """Each mined top-up's transfer plus every receipt's gasUsed."""
        return self.topups * TRANSFER_GAS + sum(
            int(r.get("gasUsed") or 0) for r in self.receipts
        )


class UserGasSponsor:
    """Tops a user's wallet up to exactly what its next transactions need, then
    sends them signed by the user's key. Built per request; the locks, the only
    state that outlives one, are module-level."""

    def __init__(self, db: DbSession, onchain: OnchainAdmin, settings: Settings):
        self._db = db
        self._onchain = onchain
        self._settings = settings

    @property
    def min_claim_micro(self) -> int:
        """Smallest claim payout worth sending, in micro-apUSD: one number for
        the claim gate and the auto-redeem scan."""
        return self._settings.min_claim_micro

    @contextmanager
    def locked(self, user: User) -> Iterator[None]:
        """Hold the user's transaction lock, or raise TransactionInProgressError
        (409) at once rather than size a second top-up on a balance the first
        is about to spend. All four kinds share it, as they share the account's
        nonce stream; callers run their on-chain gate inside it, so two claims
        cannot both pass it and burn the same tokens."""
        lock = _lock_for(user.eth_address)
        if not lock.acquire(blocking=False):
            raise TransactionInProgressError()
        try:
            yield
        finally:
            lock.release()

    def send(
        self,
        user: User,
        calls: list[ContractFunction],
        kind: SponsorKind,
        *,
        on_signed: OnSigned | None = None,
        before_send: Callable[[], None] | None = None,
    ) -> list[TxReceipt]:
        """Top `user` up to exactly what `calls` need, then send them in order,
        signed by the user's key. Must be called inside `locked(user)`.

        Every call is estimated up front on committed state, so the calls must
        not depend on each other's effects. Returns one receipt per call; if
        any reverted, `TransactionRevertedError` is raised instead, after the
        gas is booked (reverted gas is still paid).

        A top-up with no receipt in time, no free admin slot, or dropped by the
        node (`TxDropped`) is `GasTopUpTimeoutError` (503), raised before any
        user transaction goes out. `fund_gas` bounds the whole wait by
        `tx_confirmations_timeout_s`, so a jammed sender cannot hold the lock
        longer. A call the node refuses at import for its fee or for the
        balance is re-sized and retried once; a second refusal is
        `InsufficientGasError` (402) or `GasPriceMovedError` (503).

        The booking (`_book`) keeps a split/merge's reservation while anything
        may still mine unseen (see `_Paid`), the safe over-count, and otherwise
        refunds what was not paid. A late top-up needs no memory: every send
        sizes against the balance it reads.

        `on_signed(i, tx_hash)` is called each time call `i` is signed, before
        its broadcast, so the caller can record a transaction that may mine
        even if this never returns. A second hash for the same `i` means the
        node refused the first, which can never mine.

        `before_send()` is the caller's last check, once the wallet is funded
        and before the first signature, since what it checked can change while
        the top-up mines. Called once (not for a retry), with the kill switch
        off too. If it raises, nothing is sent, only the top-up is booked, and
        its error propagates.
        """
        # A cheap guard against a caller that forgot the lock. It cannot tell
        # which thread holds it, but a lock nobody holds is a sure bug.
        if not _lock_for(user.eth_address).locked():
            raise RuntimeError("UserGasSponsor.send must run inside locked(user)")
        if kind != "onboarding" and not self._settings.sponsor_user_gas:
            receipts = self._send_unsponsored(user, calls, on_signed, before_send)
        else:
            receipts = self._send_sponsored(
                user, calls, kind, on_signed, before_send
            )
        for receipt in receipts:
            if receipt["status"] != 1:
                tx_hash = receipt.get("transactionHash")
                log.warning(
                    "sponsored %s transaction %s for %s reverted",
                    kind,
                    "0x" + bytes(tx_hash).hex() if tx_hash else "?",
                    user.user_id,
                )
                raise TransactionRevertedError(
                    f"the {kind} transaction reverted on chain"
                )
        return receipts

    # --- the two paths ---------------------------------------------------

    def _send_sponsored(
        self,
        user: User,
        calls: list[ContractFunction],
        kind: SponsorKind,
        on_signed: OnSigned | None,
        before_send: Callable[[], None] | None,
    ) -> list[TxReceipt]:
        """Size, reserve, top up, check, send, book (spec §1 steps 2-6)."""
        price, limits, shortfall = self._size(user, calls)
        reserved, day = self._reserve(user, kind, limits)
        paid = _Paid()
        try:
            if shortfall:
                self._top_up(user, shortfall, paid)
            if before_send is not None:
                before_send()  # nothing is out if it raises: the reservation goes back
            retried = False  # whether call i has had its one resize-and-retry
            i = 0
            while i < len(calls):
                paid.unseen = True  # from its broadcast on, it may mine unseen
                try:
                    receipt = self._send_one(
                        user, calls[i], limits[i], price, _call_hook(on_signed, i)
                    )
                except Exception as exc:
                    # Only an answer that it can never run settles it.
                    paid.unseen = not (isinstance(exc, TxDropped) or cannot_mine(exc))
                    balance_low = is_balance_low(exc)
                    if (
                        not balance_low
                        and classify_send_error(exc) is not SendError.FEE_LOW
                    ):
                        raise
                    if retried:
                        if balance_low:
                            raise InsufficientGasError(_CANNOT_PAY) from exc
                        raise GasPriceMovedError() from exc
                    # The price rose, or the balance read was stale: size what
                    # is left again and send once more; `_book` trues it up.
                    retried = True
                    log.info(
                        "re-sizing a sponsored %s for %s after the node refused it: %s",
                        kind,
                        user.user_id,
                        exc,
                    )
                    price = self._resize(user, calls, i, limits, paid)
                    continue
                paid.unseen = False
                paid.receipts.append(receipt)
                i += 1
                # Each call has its own retry: a shared one would abort
                # onboarding with its first approvals already mined.
                retried = False
        except TxDropped as exc:
            # It never ran and never will: "busy, try again", as for a top-up.
            raise GasTopUpTimeoutError() from exc
        finally:
            self._book(user, kind, paid.gas, reserved, day, unseen=paid.unseen)
        return paid.receipts

    def _send_unsponsored(
        self,
        user: User,
        calls: list[ContractFunction],
        on_signed: OnSigned | None,
        before_send: Callable[[], None] | None,
    ) -> list[TxReceipt]:
        """The kill switch is off: send at the current price from the wallet as
        it stands, with no top-up and no booking, as the admin pays nothing. A
        wallet that cannot pay gets 402 at once."""
        price, limits = self._limits(user, calls)
        if before_send is not None:
            before_send()
        receipts: list[TxReceipt] = []
        for i, (fn, gas) in enumerate(zip(calls, limits)):
            try:
                receipts.append(
                    self._send_one(user, fn, gas, price, _call_hook(on_signed, i))
                )
            except Exception as exc:
                if is_balance_low(exc):
                    raise InsufficientGasError(_CANNOT_PAY) from exc
                raise
        return receipts

    # --- steps -----------------------------------------------------------

    def _limits(
        self, user: User, calls: list[ContractFunction]
    ) -> tuple[int, list[int]]:
        """(eth_gasPrice read once, a padded gas limit per call). skaled refunds
        the unused limit, so the pad is what the wallet keeps afterwards."""
        price = self._onchain.gas_price()
        limits = [
            self._onchain.estimate_user_gas(fn, user.eth_address)
            * (100 + GAS_BUFFER_PCT)
            // 100
            for fn in calls
        ]
        return price, limits

    def _size(
        self, user: User, calls: list[ContractFunction]
    ) -> tuple[int, list[int], int]:
        """(price, gas limits, shortfall): what `calls` need at the current
        price, less what the wallet already holds.

        A shortfall over `max_topup_gas` at that price is a wrong estimate, a
        bug, and raises RuntimeError rather than size a large transfer. The
        ceiling is in gas, so it bounds estimate bugs, not price spikes (the
        admin breaker bounds those)."""
        price, limits = self._limits(user, calls)
        need = sum(limits) * price
        shortfall = max(0, need - self._onchain.native_balance(user.eth_address))
        ceiling = self._settings.max_topup_gas * price
        if shortfall > ceiling:
            log.error(
                "refusing a %d wei top-up for %s: over the ceiling of %d gas at %d wei "
                "(limits %s) -- an estimate is wrong",
                shortfall,
                user.user_id,
                self._settings.max_topup_gas,
                price,
                limits,
            )
            raise RuntimeError(
                f"top-up of {shortfall} wei is over the ceiling of {ceiling} wei"
            )
        return price, limits, shortfall

    def _reserve(
        self, user: User, kind: SponsorKind, limits: list[int]
    ) -> tuple[int, int]:
        """Reserve a split/merge's gas, a top-up's transfer included, against
        the daily budget before anything is sent; GasBudgetExceededError (429)
        once the day is used. Returns (gas reserved, 0 when exempt; the UTC
        day). Day arithmetic and exemptions (the house, a budget of 0) are
        `OrderService._reserve_sponsored_gas`'s."""
        now = int(time.time())
        day = now // _SECONDS_PER_DAY
        budget = self._settings.daily_sponsored_gas_per_account
        if kind not in _BUDGETED or user.is_bot or not budget:
            return 0, day
        gas = sum(limits) + TRANSFER_GAS
        with self._db.write() as conn:
            accepted = TableWrite.reserve_sponsored_gas(
                conn, user.api_key, day, gas, budget
            )
        if not accepted:
            raise GasBudgetExceededError(
                retry_after=_SECONDS_PER_DAY - now % _SECONDS_PER_DAY
            )
        return gas, day

    def _top_up(self, user: User, shortfall: int, paid: _Paid) -> None:
        """Send `shortfall` and wait for it to mine. A paused breaker refuses it
        (`AdminGasPausedError`, 503), so only a wallet that needs a top-up
        meets the breaker; `TimeExhausted`, `TxUnknown` and `TxDropped` become
        `GasTopUpTimeoutError` (503).

        `paid.unseen` stays set when no answer came back (a timeout, a
        transport error, a `BaseException` mid-wait): the top-up may still
        mine. A failed connect counts as no answer, since `AdminTxSender`
        re-raises its first copy's error even when a resend got through."""
        paid.unseen = True
        try:
            self._onchain.fund_gas(
                user.eth_address,
                shortfall,
                timeout=self._settings.tx_confirmations_timeout_s,
            )
        except (TimeExhausted, TxUnknown) as exc:
            raise GasTopUpTimeoutError() from exc
        except TxDropped as exc:
            # Its nonce went to a gap filler: it never ran and never will.
            paid.unseen = False
            raise GasTopUpTimeoutError() from exc
        except Exception as exc:
            paid.unseen = classify_send_error(exc) is SendError.TRANSPORT
            raise
        paid.unseen = False
        paid.topups += 1

    def _resize(
        self,
        user: User,
        calls: list[ContractFunction],
        i: int,
        limits: list[int],
        paid: _Paid,
    ) -> int:
        """After the node refused call `i` at import: size calls `i` onwards
        again (updating `limits` in place), top up to the new need, and return
        the new price.

        Nothing of the user's is in flight here, so any failure, the ceiling's
        `RuntimeError` included, is raised as `GasTopUpTimeoutError` (503): an
        answer, which `PositionService` drops the refused row for, where a raw
        read error would pass for an unknown outcome. A paused breaker stays
        `AdminGasPausedError`."""
        try:
            price, rest, shortfall = self._size(user, calls[i:])
        except Exception as exc:
            raise GasTopUpTimeoutError() from exc
        limits[i:] = rest
        if shortfall:
            try:
                self._top_up(user, shortfall, paid)
            except DomainError:
                raise  # already an answer: busy or paused, both 503
            except Exception as exc:
                raise GasTopUpTimeoutError() from exc
        return price

    def _send_one(
        self,
        user: User,
        fn: ContractFunction,
        gas: int,
        price: int,
        on_signed: Callable[[str], None] | None,
    ) -> TxReceipt:
        """One user-signed send at the sized limit and price, so `send_user_tx`
        does not estimate again."""
        return self._onchain.send_as_user(
            user.eth_key,
            fn,
            gas=gas,
            max_fee=price,
            timeout=self._settings.tx_confirmations_timeout_s,
            on_signed=on_signed,
        )

    def _book(
        self,
        user: User,
        kind: SponsorKind,
        gas: int,
        reserved: int,
        day: int,
        *,
        unseen: bool,
    ) -> None:
        """Book what the admin paid on `day`: `gas` (mined top-ups plus every
        receipt's gasUsed, reverts included) less the reservation, so a
        split/merge is refunded what it did not use. When something may have
        mined `unseen` nothing is refunded; only an overrun is added. The house
        is never booked.

        Never fails the action: a lost booking leaves the reservation standing,
        the safe over-count."""
        if user.is_bot:
            return
        delta = gas - reserved  # reserved is 0 for claims and onboarding
        if unseen:
            delta = max(delta, 0)
        if not delta:
            return
        try:
            with self._db.write() as conn:
                TableWrite.add_sponsored_gas(conn, user.api_key, day, delta)
        except Exception:
            log.exception("booking sponsored gas failed for %s", user.user_id)
