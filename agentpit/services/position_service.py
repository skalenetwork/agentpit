import logging
import time

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
    AdminGasPausedError,
    GasPriceMovedError,
    GasTopUpTimeoutError,
    InsufficientBalanceError,
    InsufficientGasError,
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
# mined and reverted, the retry was refused (for want of balance, or for its
# fee again), or the retry's top-up could not be sized or sent (the last
# three), after the node had refused the first signature.
_ANSWERS = (
    TransactionRevertedError,
    InsufficientGasError,
    GasPriceMovedError,
    GasTopUpTimeoutError,
    AdminGasPausedError,
    TxDropped,
)


def _nothing_can_mine(exc: BaseException) -> bool:
    """Is `exc`, raised after a transaction was signed, an answer that says it
    can no longer mine? Only these are: the node refused it at import, the
    broadcast never reached the node (`chain_rpc.cannot_mine`, the test the
    sponsor books by too), or one of the sponsor's `_ANSWERS`.

    Anything else leaves it unknown, and the caller must treat it as one that
    may mine. That is not only a receipt timeout and a transport error. The
    receipt poll runs after the node took the transaction, so a JSON-RPC error
    on it, a 429 that outlives the provider's retries or a body that does not
    parse say nothing about the transaction, and `classify_send_error` (made
    for a failed broadcast) files them under OTHER."""
    return isinstance(exc, _ANSWERS) or cannot_mine(exc)


def _partition(market) -> list[int]:
    """One index set per outcome: `[1, 2]` for a binary market. Outcome i is
    `market.erc1155_tokens[i]`, so a call over this partition covers every
    token the market has. That is why a claim burns the losing tokens in the
    same transaction that pays the winner."""
    return [1 << i for i in range(len(market.erc1155_tokens))]


class PositionService:
    """User-signed split / merge / redeem against the on-chain CTF contract.

    Signed by the user's custodial key and paid for through `UserGasSponsor`,
    which tops the wallet up to exactly what the call needs just before it is
    sent, so a wallet holding no native coin at all can still split, merge and
    claim. Each action runs inside the sponsor's per-user lock, and so do its
    on-chain checks. The four kinds of user transaction share one nonce
    stream, so a second request for the same account gets a 409 instead of
    racing the first. The SPLIT / MERGE / REDEEM row is written only once the
    transaction has succeeded.

    A transaction can mine after we stopped waiting for it, so each one is
    recorded before it is broadcast: an intent row in `pending_user_txs`
    (`agentpit.services.pending_user_txs`), with the type, market and details
    its history row will have. Once the receipt is in, that row becomes the
    history row, in one statement. An answer that says it cannot mine (a
    refusal at import, a revert) deletes it. A transaction whose outcome is
    unknown (no receipt in time, no answer to the broadcast, an error from the
    receipt poll) keeps it and raises `TransactionPendingError` (503); the
    auto-redeem pass settles it from the chain later. Until then, another
    split, merge or claim by the same account on the same market is a 409, so
    a client retrying what it was told had failed does not do it twice.
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
        with self._sponsor.locked(user):
            self._refuse_while_pending(user, market_id)
            bal = self._onchain.usd_balance(user.eth_address)
            if bal < payload.amount:
                raise InsufficientBalanceError(f"need {payload.amount}, have {bal}")
            call = self._onchain.split_call(
                condition_id, _partition(market), payload.amount
            )
            details = {"amount": payload.amount}
            _receipt, tx_hash = self._send_recorded(
                user, call, "split", "SPLIT", market_id, details
            )
            self._confirm(user, "SPLIT", market_id, tx_hash, details)
        return self._snapshot(user, market, locked=payload.amount)

    def merge(
        self, user: User, market_id: int, payload: MergePositionRequest
    ) -> PositionResponse:
        market = self._require_market(market_id)
        condition_id = hex2bytes(market.condition_id.value)
        with self._sponsor.locked(user):
            self._refuse_while_pending(user, market_id)
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

        Gated on chain before anything is sent. `redeemPositions` succeeds
        with nothing to redeem, and the admin pays for the top-up in front of
        it either way. So a claim the chain would pay out less than
        `min_claim_micro` for (no tokens, only losing ones, or dust) is
        refused with `NothingToClaimError`. If the database says RESOLVED but
        the payouts were never reported on chain, the claim would revert, and
        that is a `MarketStateError`. The gate runs inside the lock, so a
        concurrent claim cannot pass it while this one burns the same tokens.

        The amount in the response and in the REDEEM row is the payout the
        chain reports in the claim's receipt (`OnchainAdmin.redeemed_payout`).
        `new_usdc_balance` is read afresh once the claim has landed and its
        row is written, so a failed read cannot lose the row.

        `payout_vector` is the market's `(payoutDenominator,
        payoutNumerators)` when the caller has already read it: the
        auto-redeem pass reads it once per market, not once per holder.
        """
        market = self._require_market(market_id)
        if market.market_state != MarketState.RESOLVED:
            raise MarketStateError("market not resolved yet")
        condition_id = hex2bytes(market.condition_id.value)
        token_ids = [int(token_id) for token_id, _label in market.erc1155_tokens]
        with self._sponsor.locked(user):
            self._refuse_while_pending(user, market_id)
            if payout_vector is None:
                payout_vector = self._onchain.payout_vector(
                    condition_id, len(token_ids)
                )
            den, nums = payout_vector
            if den == 0:
                raise MarketStateError("market is not resolved on chain yet")
            balances = self._onchain.ctf_balances(user.eth_address, token_ids)
            # Floored per outcome, as ConditionalTokens.redeemPositions pays
            # (payoutStake * numerator / den per index set) and as the
            # auto-redeem scan's `_claimable_payout` computes it.
            payout = sum(bal * num // den for bal, num in zip(balances, nums))
            # `payout <= 0` on its own, not left to the minimum: the setting is
            # validated to be at least 1, but a claim that pays nothing must
            # not reach the sponsor even if that were ever relaxed.
            if payout <= 0 or payout < self._sponsor.min_claim_micro:
                raise NothingToClaimError()
            call = self._onchain.redeem_call(condition_id, _partition(market))
            # No amount in the intent: only the receipt says what was paid.
            receipt, tx_hash = self._send_recorded(
                user, call, "claim", "REDEEM", market_id, {}
            )
            # What the CTF paid, from the claim's own receipt. Not the change
            # in the wallet's apUSD across the claim: fills, mints and
            # transfers move that without taking this lock, so the change can
            # be larger than the payout, or negative, and the profile page
            # reads a payout of zero or less as a lost market.
            paid = self._onchain.redeemed_payout(receipt, user.eth_address)
            if paid <= 0:
                # The gate expected `payout` (> 0), so a receipt with none
                # paid to the claimant is a surprise: a payout vector that
                # moved, or a redeemer other than the wallet. The REDEEM row
                # is still written, at what the receipt says.
                log.warning(
                    "claim transaction %s on market %s mined but paid the "
                    "claimant nothing; the gate expected %d",
                    tx_hash,
                    market_id,
                    payout,
                )
            self._confirm(
                user, "REDEEM", market_id, tx_hash, {"collateral_amount": paid}
            )
            new_balance = self._onchain.usd_balance(user.eth_address)
        return RedeemPositionResponse(
            market_id=market.market_id,
            collateral_amount=paid,
            new_usdc_balance=new_balance,
        )

    # --- the intent row -------------------------------------------------

    def _refuse_while_pending(self, user: User, market_id: int) -> None:
        """409 while an earlier split, merge or claim by `user` on this market
        has an outcome nobody knows yet (a pending row younger than the TTL).
        Repeating it now could split twice, or top a wallet up for a claim the
        chain has already paid. Runs first inside the lock: no chain read is
        worth making before it."""
        with self._db.read() as conn:
            pending = TableRead.has_pending_user_tx(
                conn, user.api_key, market_id, since=in_flight_since(int(time.time()))
            )
        if pending:
            raise TransactionInProgressError(
                "an earlier transaction on this market is not confirmed yet — "
                "it will appear in your history once it lands"
            )

    def _send_recorded(
        self,
        user: User,
        call: ContractFunction,
        kind: SponsorKind,
        row_type: str,
        market_id: int,
        details: dict,
    ) -> tuple[TxReceipt, str]:
        """`sponsor.send` of one call, with its intent row written just before
        each broadcast. Returns the receipt and the hash the row is under, for
        `_confirm`.

        The sponsor signs a call a second time only after the node refused the
        first signature at import (its one resize-and-retry), so the first can
        never mine and its row is replaced. When the send fails:

        - an answer that says it cannot mine (`_nothing_can_mine`): refused at
          import (402, a second fee refusal's `GasPriceMovedError`, a nonce or
          queue refusal), a broadcast that never reached the node, a retry
          whose re-sizing or top-up failed, or mined and reverted. Nothing is
          pending any more, so the row goes and the error propagates as it is.
        - anything else, once something was signed, is an unknown outcome: no
          receipt in time (`TimeExhausted`), no answer to the broadcast
          (`SendError.TRANSPORT`), or an error from the receipt poll after the
          node took the transaction. It may mine yet. The row stays, and
          `TransactionPendingError` (503) is raised. An error that is really a
          refusal but is not recognised as one lands here too, which is safe:
          the row only costs the account 409s on the market until the
          auto-redeem pass drops it for want of a receipt.

        A failure before anything was signed leaves no row and propagates as
        it is. If the row cannot be written, the hook raises and nothing is
        broadcast.
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
            (receipt,) = self._sponsor.send(user, [call], kind, on_signed=on_signed)
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
        """The transaction mined: its intent row becomes the history row, with
        `details`. If the auto-redeem pass confirmed it first, from the same
        receipt, nothing more is written.

        If the write fails, the intent row is still there and the pass writes
        the row later, so the caller hears what it would for an unknown
        outcome: `TransactionPendingError` (503), "do not repeat it"."""
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
        """Delete the intent row of a transaction that was refused or reverted.
        Called while its error is on its way to the caller, so a failure here
        is logged rather than raised over it: the reconciler drops the row
        once the TTL is up."""
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
