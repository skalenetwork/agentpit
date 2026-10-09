"""Paying the gas of transactions that a user's own key signs.

Wallets are custodial: the server holds every `User.eth_key`, and nothing
exports it. SKALE offers no protocol-level sponsorship (skaled refuses EIP-7702
transactions, there is no ERC-4337 EntryPoint, and
`ConditionalTokens.redeemPositions` redeems only `msg.sender`), so the native
coin has to be in the user's wallet before the user's transaction goes out.
`UserGasSponsor.send` works out what the calls need, has the admin send exactly
the shortfall and waits for it to mine (skaled checks the sender's balance at
import, against committed state), then sends the calls signed by the user.

The wallet never holds more than one action's need. A top-up happens only when
the balance is below the need and brings it to exactly the need, and the action
then spends part of it.

ONE WORKER ONLY. The per-user locks are module-level `threading.Lock`s, because
services are built per request and the auto-redeem pass builds its own. They
serialise one account's transactions inside this process, and that is the whole
API today (deploy/Dockerfile.api runs a single uvicorn worker, which also runs
the mirror and both resolution loops). With several workers, or several API
processes on one database, each would hold its own locks. Two of them could
then size, top up and send for one account at once, so these locks would have
to become a Postgres advisory lock (`pg_try_advisory_lock` on the address).
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
    GasBudgetExceededError,
    GasPriceMovedError,
    GasTopUpTimeoutError,
    InsufficientGasError,
    TransactionInProgressError,
    TransactionRevertedError,
)
from agentpit.onchain.admin import OnchainAdmin
from agentpit.onchain.chain_rpc import SendError, classify_send_error, is_balance_low
from agentpit.onchain.tx_sender import TRANSFER_GAS, TxDropped

log = logging.getLogger(__name__)

SponsorKind = Literal["claim", "split", "merge", "onboarding"]
# Called with (the call's index in `calls`, its transaction's hash) each time a
# call is signed, just before that transaction is broadcast.
OnSigned = Callable[[int, str], None]

_SECONDS_PER_DAY = 86_400  # the sponsored-gas budget resets at 00:00 UTC
# The node's estimate plus 20%, the pad `send_user_tx` has always used. skaled
# refunds the unused limit, so the pad is what the wallet keeps afterwards.
_GAS_BUFFER_PCT = 20
# The kinds the daily budget can refuse (owner decision 3). Claims and
# onboarding are booked to the same row but never refused for it, so heavy
# claiming can use up a day's split/merge allowance, never the other way round.
_BUDGETED = frozenset({"split", "merge"})
# 402 for a wallet that could not pay: the kill switch is off, or the node
# still said "balance too low" after the one re-top-up.
_CANNOT_PAY = "the wallet could not pay for this transaction's gas — try again later"

# One lock per account, keyed by the lowercased address and shared by every
# sponsor in the process. Entries are created under `_locks_guard`, so two
# requests cannot each create (and each hold) their own lock for one address.
# One small entry per account that ever sent: never pruned, deliberately, as a
# lock dropped while held would let a second holder in.
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
    """`send_user_tx`'s one-argument hook for call `index`: each signature of
    that call, a resized retry's included, reports its own hash under it."""
    if on_signed is None:
        return None
    return lambda tx_hash: on_signed(index, tx_hash)


class UserGasSponsor:
    """Tops a user's wallet up to exactly what its next transactions need, then
    sends them signed by the user's key.

    Built per request, like the services that call it; the only state that has
    to outlive a request, the locks, is module-level.
    """

    def __init__(self, db: DbSession, onchain: OnchainAdmin, settings: Settings):
        self._db = db
        self._onchain = onchain
        self._settings = settings

    @property
    def min_claim_micro(self) -> int:
        """Smallest claim payout worth sending, in micro-apUSD. Read from here
        by the claim gate and by the auto-redeem scan, so both use one number."""
        return self._settings.min_claim_micro

    @contextmanager
    def locked(self, user: User) -> Iterator[None]:
        """Hold the user's transaction lock, or raise TransactionInProgressError.

        Never waits. A claim button pressed twice, or auto-redeem meeting a
        manual claim, gets a 409 at once instead of a second top-up sized on a
        balance the first is about to spend. All four kinds share the lock
        because they share the account's one nonce stream. Callers run their
        on-chain gate inside it too, so two claims cannot both pass the gate
        and then burn the same tokens.
        """
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
    ) -> list[TxReceipt]:
        """Top `user` up to exactly what `calls` need, then send them in order,
        signed by the user's key. Must be called inside `locked(user)`.

        Every call is estimated up front, on committed state, so the calls must
        not depend on each other's effects: the three onboarding approvals do
        not, and claim, split and merge are one call each. Returns one receipt
        per call. If any reverted, `TransactionRevertedError` is raised instead,
        after the gas is booked, because reverted gas is still paid.

        A top-up whose receipt times out (`fund_gas` raises `TimeExhausted`,
        also when the admin sender finds no free transaction slot) stops the
        send before any user transaction goes out, and is re-raised as
        `GasTopUpTimeoutError` (503, "the platform is busy"). `fund_gas` takes
        `tx_confirmations_timeout_s` at most for the slot wait and the receipt
        wait together, so a jammed sender cannot hold the user's lock for
        longer. The top-up may still mine (unless no slot was ever found, when
        nothing was broadcast), so it is treated like a fill whose receipt
        timed out in `OrderService._book_sponsored_gas`: a split/merge
        reservation is left standing, an over-count and the safe direction,
        and nothing else is booked, not even the transfer. Nothing has to
        remember it either: every send sizes against the balance it reads, so
        once the late top-up has mined, the next send tops up only
        max(0, need - balance), which is nothing when the late top-up covers
        it. (If it has not mined yet, the next send tops up in full and the
        wallet briefly holds more than one need, which later sends use up
        before they top up again.) A top-up that can never run (`TxDropped`:
        the node lost it and a gap filler took its nonce) cannot mine later,
        so it is no timeout: it is the same retryable `GasTopUpTimeoutError`
        (503), but like a paused breaker it hands the reservation back.

        A user transaction that got no answer at all (a transport error after
        the broadcast, `SendError.TRANSPORT`) may have mined, so it books like
        a receipt timeout: the reservation stands. The error propagates as it
        is.

        The node refusing a call at import for its fee or for the wallet's
        balance is answered by one resize-and-retry of that call. If the node
        refuses the retry too, neither signature can mine and the reservation
        goes back: a second balance refusal is `InsufficientGasError` (402),
        a second fee refusal `GasPriceMovedError` (503, "try again").

        `on_signed(i, tx_hash)` is called each time call `i` is signed, before
        its transaction is broadcast, so the caller can record a transaction
        that may mine even if this never returns. A call is signed again only
        after the node refused it at import (its one resize-and-retry: each
        call has its own), so a second hash for the same `i` means the first
        can never mine.
        """
        # A cheap guard against a caller that forgot the lock. It cannot tell
        # which thread holds it, but a lock nobody holds is a sure bug.
        if not _lock_for(user.eth_address).locked():
            raise RuntimeError("UserGasSponsor.send must run inside locked(user)")
        if kind != "onboarding" and not self._settings.sponsor_user_gas:
            receipts = self._send_unsponsored(user, calls, on_signed)
        else:
            receipts = self._send_sponsored(user, calls, kind, on_signed)
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
    ) -> list[TxReceipt]:
        """Size, reserve, top up, send, book (spec §1 steps 2-6)."""
        price, limits, shortfall = self._size(user, calls)
        reserved, day = self._reserve(user, kind, limits)
        receipts: list[TxReceipt] = []
        topups = 0
        timed_out = False
        try:
            if shortfall:
                self._top_up(user, shortfall)
                topups += 1
            retried = False  # whether call i has had its one resize-and-retry
            i = 0
            while i < len(calls):
                try:
                    receipts.append(
                        self._send_one(
                            user, calls[i], limits[i], price, _call_hook(on_signed, i)
                        )
                    )
                except Exception as exc:
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
                    # The price rose since sizing, or the balance read was
                    # stale: size what is left again and send once more. The
                    # reservation stays as it is; the booking below trues it up.
                    retried = True
                    log.info(
                        "re-sizing a sponsored %s for %s after the node refused it: %s",
                        kind,
                        user.user_id,
                        exc,
                    )
                    price, rest, shortfall = self._size(user, calls[i:])
                    limits[i:] = rest
                    if shortfall:
                        self._top_up(user, shortfall)
                        topups += 1
                    continue
                i += 1
                # Each call gets its own retry: the fee can rise again between
                # onboarding's approvals, and a shared one would abort the
                # batch with the first approvals already mined.
                retried = False
        except TxDropped as exc:
            # The top-up's nonce went to a gap filler or another writer's
            # transaction: it never ran and never will, so this is no timeout
            # and the reservation is refunded. It answers like one, because to
            # the caller it is the same: our side is busy, try again. Raised
            # from a handler, so the clause below does not catch it.
            raise GasTopUpTimeoutError() from exc
        except (TimeExhausted, GasTopUpTimeoutError):
            # No receipt in time, for a top-up or for a user transaction: it
            # may still mine, so the booking must not refund the reservation.
            timed_out = True
            raise
        except Exception as exc:
            # No answer to a broadcast (a reset, a proxy's 502): the node may
            # hold the transaction and mine it, the same unknown as a timeout.
            # Any other failure is an answer, and a refusal ran nothing.
            if classify_send_error(exc) is SendError.TRANSPORT:
                timed_out = True
            raise
        finally:
            gas = topups * TRANSFER_GAS + sum(
                int(r.get("gasUsed") or 0) for r in receipts
            )
            self._book(user, kind, gas, reserved, day, timed_out=timed_out)
        return receipts

    def _send_unsponsored(
        self,
        user: User,
        calls: list[ContractFunction],
        on_signed: OnSigned | None,
    ) -> list[TxReceipt]:
        """The kill switch is off: send at the current price from the wallet as
        it stands. No balance read, no reservation, no top-up, no booking,
        because the admin pays nothing. A wallet that cannot pay gets 402 at
        once; the retry exists only to top up again, and that is switched off.
        """
        price, limits = self._limits(user, calls)
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
        """(eth_gasPrice read once, a gas limit per call). The estimates carry
        no fee fields: with one, anvil refuses to estimate for a dry wallet
        ("gas required exceeds allowance: 0")."""
        price = self._onchain.gas_price()
        limits = [
            self._onchain.estimate_user_gas(fn, user.eth_address)
            * (100 + _GAS_BUFFER_PCT)
            // 100
            for fn in calls
        ]
        return price, limits

    def _size(
        self, user: User, calls: list[ContractFunction]
    ) -> tuple[int, list[int], int]:
        """(price, gas limits, shortfall): what `calls` need at the current
        price, less what the wallet already holds.

        Raises RuntimeError over the ceiling. That is a bug, not a user error:
        a wrong estimate must never size a large transfer from the admin."""
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
        """Reserve a split/merge's gas against the daily budget before anything
        is sent; refuse with GasBudgetExceededError (429) once the day is used.

        Returns (what was reserved, 0 when nothing applies; the UTC day it sits
        on). The day arithmetic is `OrderService._reserve_sponsored_gas`'s, and
        so are the exemptions: the house (`is_bot`) and a budget of 0. The
        top-up's transfer is reserved too, whether or not one turns out to be
        needed; the booking trues it up."""
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

    def _top_up(self, user: User, shortfall: int) -> None:
        """Send `shortfall` and wait for it to mine: skaled checks the balance
        at import, against committed state. A sponsored admin send, so a paused
        breaker refuses it (`AdminGasPausedError`, 503). Only a wallet that
        needs a top-up meets the breaker; a funded one proceeds while paused.
        `TimeExhausted` (no receipt in time, or no free admin slot) becomes
        `GasTopUpTimeoutError` (503). `TxDropped` becomes one too, but in
        `_send_sponsored`, which has to tell the two apart for the booking.
        `send`'s docstring says what each does to the booking."""
        try:
            self._onchain.fund_gas(
                user.eth_address,
                shortfall,
                timeout=self._settings.tx_confirmations_timeout_s,
            )
        except TimeExhausted as exc:
            raise GasTopUpTimeoutError() from exc

    def _send_one(
        self,
        user: User,
        fn: ContractFunction,
        gas: int,
        price: int,
        on_signed: Callable[[str], None] | None,
    ) -> TxReceipt:
        """One user-signed send at the sized limit and price, so `send_user_tx`
        does not estimate again (one more ~0.5 s round trip on SKALE)."""
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
        timed_out: bool,
    ) -> None:
        """Book what the admin really paid on `day`, under the rules of
        `OrderService._book_sponsored_gas`.

        `gas` is each mined top-up's transfer plus every receipt's gasUsed,
        reverted ones included. A split/merge is booked that minus its reservation: a
        refund when the estimate was high, all of it when nothing was sent.
        After a receipt timeout (a top-up's or a user transaction's) nothing is
        refunded, since the transaction may have mined unseen; only an overrun
        is added. A timed-out top-up is not in `gas`, so it adds nothing.
        Claims and onboarding reserved nothing and are booked in full. The
        house (`is_bot`) is never booked.

        Never fails the action: by now the transactions are on chain (or have
        failed, and this runs from `finally` all the same). A lost booking
        leaves a split/merge's reservation standing, which over-counts, the
        safe direction."""
        if user.is_bot:
            return
        delta = gas - reserved  # reserved is 0 for claims and onboarding
        if timed_out:
            delta = max(delta, 0)
        if not delta:
            return
        try:
            with self._db.write() as conn:
                TableWrite.add_sponsored_gas(conn, user.api_key, day, delta)
        except Exception:
            log.exception("booking sponsored gas failed for %s", user.user_id)
