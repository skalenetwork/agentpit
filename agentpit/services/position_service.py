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
    InsufficientBalanceError,
    MarketNotFoundError,
    MarketStateError,
    NothingToClaimError,
)
from agentpit.onchain.admin import OnchainAdmin
from agentpit.services.gas_sponsor import UserGasSponsor
from agentpit.utils.parse import hex2bytes


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
            bal = self._onchain.usd_balance(user.eth_address)
            if bal < payload.amount:
                raise InsufficientBalanceError(f"need {payload.amount}, have {bal}")
            call = self._onchain.split_call(
                condition_id, _partition(market), payload.amount
            )
            self._sponsor.send(user, [call], "split")
            with self._db.write() as conn:
                TableWrite.log_transaction(
                    conn, user.api_key, "SPLIT", market_id, {"amount": payload.amount}
                )
        return self._snapshot(user, market, locked=payload.amount)

    def merge(
        self, user: User, market_id: int, payload: MergePositionRequest
    ) -> PositionResponse:
        market = self._require_market(market_id)
        condition_id = hex2bytes(market.condition_id.value)
        with self._sponsor.locked(user):
            for token_id, _label in market.erc1155_tokens:
                bal = self._onchain.ctf_balance(user.eth_address, int(token_id))
                if bal < payload.amount:
                    raise InsufficientBalanceError(
                        f"need {payload.amount} of token {token_id}, have {bal}"
                    )
            call = self._onchain.merge_call(
                condition_id, _partition(market), payload.amount
            )
            self._sponsor.send(user, [call], "merge")
            with self._db.write() as conn:
                TableWrite.log_transaction(
                    conn, user.api_key, "MERGE", market_id, {"amount": payload.amount}
                )
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
        `new_usdc_balance` is read afresh once the claim has landed.

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
            (receipt,) = self._sponsor.send(user, [call], "claim")
            # What the CTF paid, from the claim's own receipt. Not the change
            # in the wallet's apUSD across the claim: fills, mints and
            # transfers move that without taking this lock, so the change can
            # be larger than the payout, or negative, and the profile page
            # reads a payout of zero or less as a lost market.
            paid = self._onchain.redeemed_payout(receipt, user.eth_address)
            new_balance = self._onchain.usd_balance(user.eth_address)
            with self._db.write() as conn:
                TableWrite.log_transaction(
                    conn, user.api_key, "REDEEM", market_id,
                    {"collateral_amount": paid},
                )
        return RedeemPositionResponse(
            market_id=market.market_id,
            collateral_amount=paid,
            new_usdc_balance=new_balance,
        )

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
