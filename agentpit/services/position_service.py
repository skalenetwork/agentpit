import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from web3.contract.contract import ContractFunction
from web3.types import TxReceipt

from agentpit.datastructures.market_state import MarketState
from agentpit.datastructures.position_response import PositionResponse
from agentpit.datastructures.redeem_position_response import RedeemPositionResponse
from agentpit.datastructures.split_position_request import (
    MergePositionRequest,
    SplitPositionRequest,
)
from agentpit.datastructures.user import User
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import (
    SPONSORED_GAS_REFUSALS,
    InsufficientBalanceError,
    MarketNotFoundError,
    MarketStateError,
    NothingToClaimError,
    TransactionInProgressError,
    TransactionPendingError,
    TransactionRevertedError,
)
from agentpit.onchain.admin import OnchainAdmin
from agentpit.onchain.chain_rpc import cannot_mine
from agentpit.onchain.tx_sender import TxDropped
from agentpit.services.gas_sponsor import SponsorKind, UserGasSponsor
from agentpit.services.pending_user_txs import in_flight_since
from agentpit.utils.parse import hex2bytes

log = logging.getLogger(__name__)

# What the sponsor raises once a signed transaction can no longer mine: it
# mined and reverted, or after the node refused it, its retry was refused too
# or could not be funded.
_ANSWERS = (TransactionRevertedError, TxDropped, *SPONSORED_GAS_REFUSALS)


def _nothing_can_mine(exc: BaseException) -> bool:
    """Is `exc`, raised after a transaction was signed, an answer that it can
    no longer mine: a refusal at import, a broadcast that never reached the
    node (`chain_rpc.cannot_mine`, which the sponsor books by too), or one of
    `_ANSWERS`? Anything else, a receipt-poll error included, may still mine."""
    return isinstance(exc, _ANSWERS) or cannot_mine(exc)


def claimable_payout(
    balances: list[int], den: int, nums: list[int], minimum: int
) -> int:
    """What `redeemPositions` pays for `balances` under the payout vector
    `(den, nums)`, floored per outcome as the CTF floors it, or 0 when that is
    under `minimum` (or nothing at all, whatever the minimum). `den` must not
    be 0. The claim gate of `PositionService.redeem` and the auto-redeem scan."""
    payout = sum(bal * num // den for bal, num in zip(balances, nums))
    return payout if payout >= max(minimum, 1) else 0


def _partition(market) -> list[int]:
    """One index set per outcome (`[1, 2]` for a binary market), so a call
    over it covers every token the market has: a claim burns the losing
    tokens in the same transaction that pays the winner."""
    return [1 << i for i in range(len(market.erc1155_tokens))]


class PositionService:
    """User-signed split / merge / redeem against the on-chain CTF contract.

    Paid for through `UserGasSponsor`, which tops the wallet up to exactly what
    the call needs, so an empty wallet can still act. Each action and its
    on-chain checks run inside the sponsor's per-user lock: the account's
    transactions share one nonce stream, so a concurrent request gets a 409.

    A transaction can mine after we stopped waiting for it, so an intent row
    goes into `pending_user_txs` before each broadcast and becomes the
    history row once the receipt is in (`agentpit.services.pending_user_txs`).
    An unknown outcome keeps the row and raises `TransactionPendingError`
    (503); until the auto-redeem pass settles it, another split, merge or
    claim by the account on that market is a 409.
    """

    def __init__(self, db: DbSession, onchain: OnchainAdmin, sponsor: UserGasSponsor):
        self._db = db
        self._onchain = onchain
        self._sponsor = sponsor

    def split(
        self, user: User, market_id: int, payload: SplitPositionRequest
    ) -> PositionResponse:
        market = self._require_active_market(market_id)
        condition_id = hex2bytes(market.condition_id.value)
        with self._locked(user, market_id):
            bal = self._onchain.usd_balance(user.eth_address)
            if bal < payload.amount:
                raise InsufficientBalanceError(f"need {payload.amount}, have {bal}")
            call = self._onchain.split_call(
                condition_id, _partition(market), payload.amount
            )
            details = {"amount": payload.amount}
            _receipt, tx_hash = self._send_recorded(
                user,
                call,
                "split",
                "SPLIT",
                market_id,
                details,
                # The top-up can take a while: check the market again once the
                # wallet is funded, before anything is signed.
                before_send=lambda: self._require_active_market(market_id),
            )
            self._confirm(user, "SPLIT", market_id, tx_hash, details)
        return self._snapshot(user, market, locked=payload.amount)

    def merge(
        self, user: User, market_id: int, payload: MergePositionRequest
    ) -> PositionResponse:
        market = self._require_market(market_id)
        condition_id = hex2bytes(market.condition_id.value)
        with self._locked(user, market_id):
            for token_id, _label in market.erc1155_tokens:
                bal = self._onchain.ctf_balance(user.eth_address, int(token_id))
                if bal < payload.amount:
                    raise InsufficientBalanceError(
                        f"need {payload.amount} of token {token_id}, have {bal}"
                    )
            call = self._onchain.merge_call(
                condition_id, _partition(market), payload.amount
            )
            details = {"amount": payload.amount}
            _receipt, tx_hash = self._send_recorded(
                user, call, "merge", "MERGE", market_id, details
            )
            self._confirm(user, "MERGE", market_id, tx_hash, details)
        return self._snapshot(user, market, unlocked=payload.amount)

    def redeem(
        self,
        user: User,
        market_id: int,
        *,
        payout_vector: tuple[int, list[int]] | None = None,
    ) -> RedeemPositionResponse:
        """Claim `user`'s payout on a resolved market.

        Gated on chain, inside the lock, before anything is sent:
        `redeemPositions` succeeds with nothing to redeem and the admin pays
        for it either way, so a payout under `min_claim_micro` is
        `NothingToClaimError`, and payouts never reported on chain (the claim
        would revert) are a `MarketStateError`. The gate runs again after the
        top-up, as `before_send`, since the tokens can leave while it mines.

        The amount reported and written is what the CTF's `PayoutRedemption`
        in the receipt paid (`OnchainAdmin.redeemed_payout`); a claim that
        mined and paid nothing writes no row and is `NothingToClaimError` too.
        `payout_vector` lets the auto-redeem pass read `(payoutDenominator,
        payoutNumerators)` once per market rather than once per holder.
        """
        market = self._require_market(market_id)
        if market.market_state != MarketState.RESOLVED:
            raise MarketStateError("market not resolved yet")
        condition_id = hex2bytes(market.condition_id.value)
        token_ids = [int(token_id) for token_id, _label in market.erc1155_tokens]
        with self._locked(user, market_id):
            if payout_vector is None:
                payout_vector = self._onchain.payout_vector(
                    condition_id, len(token_ids)
                )
            den, nums = payout_vector
            if den == 0:
                raise MarketStateError("market is not resolved on chain yet")
            payout = self._claimable(user, token_ids, den, nums)
            call = self._onchain.redeem_call(condition_id, _partition(market))
            # No amount in the intent: only the receipt says what was paid.
            receipt, tx_hash = self._send_recorded(
                user,
                call,
                "claim",
                "REDEEM",
                market_id,
                {},
                # The tokens can leave while the top-up mines (a resting SELL
                # filled by matchOrders). A reported payout vector is final,
                # so only the balances are read again.
                before_send=lambda: self._claimable(user, token_ids, den, nums),
            )
            # From the receipt, not the change in the wallet's apUSD: fills,
            # mints and transfers move that without this lock, so it can be
            # larger than the payout or negative.
            paid = self._onchain.redeemed_payout(receipt, user.eth_address)
            if paid <= 0:
                # The tokens left after the last check, or the payout moved.
                # No claim was made: a REDEEM row at zero would read as a lost
                # market and as a claim made. The gas stays booked.
                log.warning(
                    "claim transaction %s on market %s mined but paid the "
                    "claimant nothing; the gate expected %d",
                    tx_hash,
                    market_id,
                    payout,
                )
                self._forget(tx_hash)
                raise NothingToClaimError()
            self._confirm(
                user, "REDEEM", market_id, tx_hash, {"collateral_amount": paid}
            )
            new_balance = self._onchain.usd_balance(user.eth_address)
        return RedeemPositionResponse(
            market_id=market.market_id,
            collateral_amount=paid,
            new_usdc_balance=new_balance,
        )

    def _claimable(
        self, user: User, token_ids: list[int], den: int, nums: list[int]
    ) -> int:
        """The claim gate: what the chain would pay `user` for its tokens of
        this market, or `NothingToClaimError` under the sponsor's minimum."""
        balances = self._onchain.ctf_balances(user.eth_address, token_ids)
        payout = claimable_payout(balances, den, nums, self._sponsor.min_claim_micro)
        if not payout:
            raise NothingToClaimError()
        return payout

    # --- the intent row -------------------------------------------------

    @contextmanager
    def _locked(self, user: User, market_id: int) -> Iterator[None]:
        """The sponsor's per-user lock, refused with a 409 while an earlier
        split, merge or claim by `user` on this market has an unknown outcome
        (a pending row younger than the TTL): repeating it could split twice,
        or top a wallet up for a claim already paid."""
        with self._sponsor.locked(user):
            with self._db.read() as conn:
                pending = TableRead.has_pending_user_tx(
                    conn,
                    user.api_key,
                    market_id,
                    since=in_flight_since(int(time.time())),
                )
            if pending:
                raise TransactionInProgressError(
                    "an earlier transaction on this market is not confirmed yet — "
                    "it will appear in your history once it lands"
                )
            yield

    def _send_recorded(
        self,
        user: User,
        call: ContractFunction,
        kind: SponsorKind,
        row_type: str,
        market_id: int,
        details: dict,
        *,
        before_send: Callable[[], None] | None = None,
    ) -> tuple[TxReceipt, str]:
        """`sponsor.send` of one call, its intent row written just before each
        broadcast; a resized retry's row replaces the refused signature's.
        Returns the receipt and the row's hash, for `_confirm`.

        A failure before anything was signed (`before_send`'s included)
        leaves no row and propagates. After a signature, an answer that it
        cannot mine (`_nothing_can_mine`) drops the row and propagates;
        anything else may still mine, so the row stays and
        `TransactionPendingError` (503) is raised. An unrecognised refusal
        lands there too, which is safe: it only costs the account 409s on the
        market until the row is dropped as lost.
        """
        signed: list[str] = []

        def on_signed(_index: int, tx_hash: str) -> None:
            with self._db.write() as conn:
                if signed:
                    TableWrite.delete_pending_user_tx(conn, signed[-1])
                TableWrite.insert_pending_user_tx(
                    conn,
                    tx_hash,
                    user.api_key,
                    row_type,
                    market_id,
                    details,
                    created_at=int(time.time()),
                )
            signed.append(tx_hash)

        try:
            (receipt,) = self._sponsor.send(
                user, [call], kind, on_signed=on_signed, before_send=before_send
            )
        except Exception as exc:
            if not signed:
                raise
            if _nothing_can_mine(exc):
                self._forget(signed[-1])
                raise
            log.warning(
                "%s transaction %s of %s on market %s was sent and its "
                "outcome is unknown (%s); kept as pending",
                row_type,
                signed[-1],
                user.user_id,
                market_id,
                exc,
            )
            raise TransactionPendingError() from exc
        if not signed:  # the sponsor signs every call it sends
            raise RuntimeError("the sponsor sent a transaction without its hash")
        return receipt, signed[-1]

    def _confirm(
        self, user: User, row_type: str, market_id: int, tx_hash: str, details: dict
    ) -> None:
        """The transaction mined: its intent row becomes the history row (once,
        even if the reconciler races it). If the write fails, the intent row
        stays for the pass, so the caller gets `TransactionPendingError`."""
        try:
            with self._db.write() as conn:
                TableWrite.confirm_pending_user_tx(conn, tx_hash, details)
        except Exception as exc:
            log.exception(
                "%s transaction %s of %s on market %s mined, but its row could "
                "not be written; the auto-redeem pass writes it from the "
                "pending row",
                row_type,
                tx_hash,
                user.user_id,
                market_id,
            )
            raise TransactionPendingError() from exc

    def _forget(self, tx_hash: str) -> None:
        """Drop the intent row of a refused or reverted transaction. A failure
        is logged, not raised over the error on its way out: the reconciler
        drops the row once the TTL is up."""
        try:
            with self._db.write() as conn:
                TableWrite.delete_pending_user_tx(conn, tx_hash)
        except Exception:
            log.exception("could not drop the pending row of %s", tx_hash)

    # --- helpers --------------------------------------------------------

    def _require_market(self, market_id: int):
        with self._db.read() as conn:
            market = TableRead.read_market(conn, market_id)
        if market is None:
            raise MarketNotFoundError(market_id)
        if market.condition_id is None:
            raise MarketStateError("market has no on-chain condition_id")
        return market

    def _require_active_market(self, market_id: int):
        """`_require_market`, plus: split only while the market trades. After
        resolution a split mints a pair whose loser is worthless and whose winner
        is redeemable, which is a free way to manufacture claims. Merge is not
        guarded: it is how holders recover collateral from YES+NO pairs on a
        cancelled market, so it runs in any state. Its gas is sponsored like a
        split's and counts against the same daily budget, which is what bounds
        a merge loop."""
        market = self._require_market(market_id)
        if market.market_state != MarketState.ACTIVE:
            raise MarketStateError("split only runs on ACTIVE markets")
        return market

    def _snapshot(self, user: User, market, *, locked: int = 0, unlocked: int = 0):
        balances = {}
        for token_id, _label in market.erc1155_tokens:
            balances[token_id] = self._onchain.ctf_balance(
                user.eth_address, int(token_id)
            )
        return PositionResponse(
            market_id=market.market_id,
            amount=locked or unlocked,
            collateral_amount=locked,
            token_balances=balances,
        )
