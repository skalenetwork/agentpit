"""Restoring a user's paper balance to the target, at most once a day."""
import time

from pydantic import BaseModel

from agentpit.config import Settings
from agentpit.datastructures.user import User
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.onchain.admin import OnchainAdmin
from agentpit.services.account_service import AccountService
from agentpit.services.deployment_reset import reconcile_deployment


class TopUpResult(BaseModel):
    balance_raw: int
    minted_raw: int
    next_allowed_at: int


def topup_amount_raw(balance_raw: int, target_raw: int) -> int:
    """How much to mint so the account lands exactly on the target.

    Zero when the balance is already there or beyond: this restores a demo
    balance, it does not hand out a fixed sum. A flat grant would pay more to
    someone who lost everything than to someone who did well.
    """
    return max(0, target_raw - balance_raw)


def next_allowed_at(last_topup_at: int | None, cooldown_seconds: int) -> int:
    """Unix time when the next top-up becomes allowed; 0 when it already is."""
    if last_topup_at is None:
        return 0
    return last_topup_at + cooldown_seconds


class BalanceService:
    def __init__(
        self,
        db: DbSession,
        onchain: OnchainAdmin,
        settings: Settings,
        accounts: AccountService,
    ):
        self._db = db
        self._onchain = onchain
        self._settings = settings
        self._accounts = accounts

    def next_allowed(self, user: User) -> int:
        """Cooldown status only: no chain call, no mint, no write.

        Backs the GET side of the top-up endpoint, so the button knows when
        it may be clicked without paying for an RPC round-trip or touching
        LAST_TOPUP_AT the way a real top-up attempt would. A competition entry
        past its lock pauses top-ups until its end value is taken.
        """
        with self._db.read() as conn:
            last = TableRead.get_last_topup_at(conn, user.user_id)
            paused = TableRead.topup_paused_until(conn, user.user_id, int(time.time()))
        return max(next_allowed_at(last, self._settings.topup_cooldown_seconds), paused or 0)

    def _net_worth_raw(self, address: str) -> int:
        """Cash plus what the open positions are currently worth, raw.

        Cash alone is what made the top-up farmable: move it into positions and
        the shortfall reads as the whole grant, so pressing the button daily
        grew net worth without limit. What a demo balance restores is what the
        account is worth, and positions are part of that.

        `total_value` walks positions on chain, which is why this is reached
        only from `top_up` -- a human-initiated call at most once a day -- and
        never from `next_allowed`, which the profile page issues on load.
        """
        cash = self._onchain.usd_balance(address)
        rows = self._accounts.total_value(address)
        value_whole = rows[0]["value"] if rows else 0.0
        return cash + int(round(value_whole * 10**6))

    def top_up(self, user: User, now: int) -> TopUpResult:
        # Net worth first: every early return below still needs to report it.
        # This is cash PLUS positions -- see _net_worth_raw for why.
        balance = self._net_worth_raw(user.eth_address)

        with self._db.read() as conn:
            last = TableRead.get_last_topup_at(conn, user.user_id)
            paused = TableRead.topup_paused_until(conn, user.user_id, now)
        allowed_at = max(next_allowed_at(last, self._settings.topup_cooldown_seconds), paused or 0)
        if paused is not None:
            return TopUpResult(balance_raw=balance, minted_raw=0, next_allowed_at=allowed_at)

        minted = topup_amount_raw(balance, self._settings.paper_balance_target_raw)
        if minted == 0:
            # Nothing to restore. Not a failure, and it must not start the
            # cooldown — otherwise checking while ahead costs you the day.
            return TopUpResult(
                balance_raw=balance, minted_raw=0, next_allowed_at=allowed_at
            )

        # See deployment_reset.reconcile_deployment for why this runs on
        # every top-up.
        with self._db.write() as conn:
            reconcile_deployment(conn, user.user_id, self._onchain.deployment_id)

        # Claim the day atomically before minting, so the claim can never be
        # outrun by a concurrent request. The predicate reads the row's own
        # current value rather than trusting `last`, which may already be
        # stale by the time we get here.
        not_before = now - self._settings.topup_cooldown_seconds
        with self._db.write() as conn:
            claimed = TableWrite.claim_topup(
                conn,
                user.user_id,
                now,
                not_before,
                minted,
                self._settings.paper_balance_target_raw,
            )
        if not claimed:
            # We lost the claim, which only happens once someone else's
            # winning write has already committed (an UPDATE re-checks its
            # WHERE clause after any conflicting row lock releases). `last`
            # is now stale, so re-read to report the real next_allowed_at —
            # the loser of a race must not be told it can retry immediately.
            with self._db.read() as conn:
                current_last = TableRead.get_last_topup_at(conn, user.user_id)
            return TopUpResult(
                balance_raw=balance,
                minted_raw=0,
                next_allowed_at=next_allowed_at(
                    current_last, self._settings.topup_cooldown_seconds
                ),
            )

        try:
            self._onchain.mint_to(
                user.eth_address,
                minted,
                timeout=self._settings.tx_confirmations_timeout_s,
            )
        except Exception:
            # The mint never landed: release the claim so a failed mint does
            # not cost the user their day. A successful mint, by contrast,
            # must never be left unrecorded — which is why the claim is
            # taken before, not after, the mint.
            with self._db.write() as conn:
                TableWrite.release_topup(conn, user.user_id, last, minted)
            raise

        return TopUpResult(
            balance_raw=balance + minted,
            minted_raw=minted,
            next_allowed_at=now + self._settings.topup_cooldown_seconds,
        )
