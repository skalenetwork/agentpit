"""Idempotent provisioning of the engine's house (bot) accounts."""
import logging

from agentpit.auth.passwords import hash_password
from agentpit.config import Settings
from agentpit.datastructures.user import User
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import AdminGasPausedError
from agentpit.onchain.admin import OnchainAdmin
from agentpit.onchain.deployment import is_disposable_chain

log = logging.getLogger(__name__)

# The pad `send_user_tx` puts on an estimate, which is what the approvals are
# sent with. `_fund` sizes the gas for them with the same one.
_GAS_PAD_PCT = 20
_EMAIL = "house-bot-{i}@agentpit.local"
_PASSWORD = "house-bot-fixed-secret-pw"  # house accounts never log in via HTTP


def email_for(i: int) -> str:
    """Deterministic house-account email; index 0 is the mirror account."""
    return _EMAIL.format(i=i)


def gas_topup_wei(balance_wei: int, floor_wei: int, target_wei: int) -> int:
    """Wei to send so the account sits at `target_wei`; 0 while it is above the floor.

    Triggering on a floor rather than on exhaustion is the whole point. The
    account signs a transaction per inventory split, so it drains steadily, and
    it cannot pay for the transaction that would refill it once it is empty --
    production stalled on dust (0.0000112 ETH), a balance that is starved but
    emphatically not zero.
    """
    if floor_wei <= 0 or balance_wei >= floor_wei:
        return 0
    return max(0, target_wei - balance_wei)


class HouseAccountProvisioner:
    def __init__(self, db: DbSession, onchain: OnchainAdmin, settings: Settings):
        self._db = db
        self._onchain = onchain
        self._settings = settings

    def ensure_provisioned(self) -> list[User]:
        target = self._settings.liquidity_house_account_count
        with self._db.read() as conn:
            existing = {u.email: u for u in TableRead.list_bot_users(conn)}

        for u in existing.values():           # re-onboard accounts the chain forgot
            self._maybe_reonboard(u)

        users: list[User] = list(existing.values())
        for i in range(target):
            email = email_for(i)
            if email in existing:
                continue
            users.append(self._create_and_onboard(email))
        log.info("house accounts: %d provisioned (target %d)", len(users), target)
        return users

    def _create_and_onboard(self, email: str) -> User:
        with self._db.write() as conn:
            prior = TableRead.get_user_by_email(conn, email)
            if prior is not None:             # partial-create recovery
                user_id, acct, api_key = prior.user_id, prior.eth_key, prior.api_key
            else:
                user_id, acct, api_key = TableWrite.create_user(
                    conn, email=email, password_hash=hash_password(_PASSWORD), handle=None
                )
        self._fund(acct)
        with self._db.write() as conn:
            TableWrite.mark_user_onboarded(conn, user_id)
            TableWrite.mark_user_as_bot(conn, api_key)
        with self._db.read() as conn:
            user = TableRead.get_user_by_userid(conn, user_id)
        assert user is not None
        return user

    def _approvals_need_wei(self, address: str) -> int:
        """What the house's three approvals cost: each one's estimate plus the
        pad `send_user_tx` adds, at the current price (the `maxFeePerGas` they
        go out with)."""
        gas = sum(
            self._onchain.estimate_user_gas(fn, address) * (100 + _GAS_PAD_PCT) // 100
            for fn in self._onchain.approval_calls()
        )
        return gas * self._onchain.gas_price()

    def _fund(self, acct) -> None:
        """Mint the collateral, fund the gas AT the floor, send the approvals.

        The floor, not the target. `top_up_gas` lifts the account to the
        target within one check interval anyway. Funding a fresh account
        straight to the target (100 native by default) would make every
        provisioning on the persistent local anvil cost the admin 100 native,
        and the test suite provisions on every run. There is no user signup
        grant to reuse any more: users get exact per-transaction top-ups
        (`UserGasSponsor`). The house signs its own mirror splits, so it needs
        a standing balance instead.

        Never less than the approvals need, though: a floor of 0 (which also
        switches `top_up_gas` off) or a tiny one would leave the house unable
        to pay for the three approvals sent right below, and provisioning
        would fail startup.
        """
        timeout = self._settings.tx_confirmations_timeout_s
        self._onchain.mint_to(
            acct.address, self._settings.house_mint_raw, timeout=timeout
        )
        self._onchain.fund_gas(
            acct.address,
            max(
                self._settings.liquidity_gas_floor_wei,
                self._approvals_need_wei(acct.address),
            ),
            timeout=timeout,
        )
        self._onchain.grant_user_approvals(acct, timeout=timeout)

    def top_up_gas(self, users: list[User]) -> int:
        """Refill any house account that has dropped below the gas floor.

        Gas ONLY. `_fund` also drips collateral and re-sends the three approvals,
        which is right for a fresh or chain-reset account and wrong for a routine
        refill -- the approvals are already set, and each one costs the very gas
        we are short of. Returns the number of accounts funded.
        """
        floor = self._settings.liquidity_gas_floor_wei
        target = self._settings.liquidity_gas_target_wei
        funded = 0
        for user in users:
            try:
                balance = self._onchain.native_balance(user.eth_address)
            except Exception as exc:
                log.warning("gas balance check failed for %s: %s", user.email, exc)
                continue
            add = gas_topup_wei(balance, floor, target)
            if add <= 0:
                continue
            try:
                self._onchain.fund_gas(
                    user.eth_address, add,
                    timeout=self._settings.tx_confirmations_timeout_s,
                )
            except AdminGasPausedError:
                log.warning("gas top-up for %s skipped: the admin gas breaker is paused", user.email)
                continue
            except Exception:
                log.exception("gas top-up failed for %s", user.email)
                continue
            log.info(
                "house account %s gas topped up: %.4f -> %.4f ETH",
                user.email, balance / 1e18, (balance + add) / 1e18,
            )
            funded += 1
        return funded

    def _maybe_reonboard(self, user: User) -> None:
        """Repair an account the chain forgot — a wipe, not ordinary spending.

        Gated on `simulated_chain` and on the chain id (`is_disposable_chain`) for
        the same reason as the user-facing path: only a disposable chain can
        forget a funded account. Routine depletion is `top_up_gas`'s job, and it
        triggers on a floor rather than on zero.
        """
        if not self._settings.simulated_chain or not is_disposable_chain(
            self._onchain.chain_id
        ):
            return
        try:
            if self._onchain.native_balance(user.eth_address) > 0:
                return
        except Exception as exc:
            log.warning("native balance check failed for %s: %s", user.user_id, exc)
            return
        log.info("house account %s unfunded (chain reset) — re-onboarding", user.email)
        try:
            self._fund(user.eth_key)
        except Exception:
            log.exception("re-onboarding house account %s failed", user.email)
