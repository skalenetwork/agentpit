"""Idempotent provisioning of the house account."""

import logging

from agentpit.auth.passwords import hash_password
from agentpit.config import Settings
from agentpit.datastructures.user import User
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.onchain.admin import OnchainAdmin
from agentpit.onchain.deployment import is_disposable_chain
from agentpit.onchain.user_wallet import GAS_BUFFER_PCT

log = logging.getLogger(__name__)

_EMAIL = "house-bot-0@agentpit.local"
_PASSWORD = "house-bot-fixed-secret-pw"  # house accounts never log in via HTTP


class HouseAccountProvisioner:
    def __init__(self, db: DbSession, onchain: OnchainAdmin, settings: Settings):
        self._db = db
        self._onchain = onchain
        self._settings = settings

    def ensure_provisioned(self) -> User:
        with self._db.read() as conn:
            user = TableRead.get_user_by_email(conn, _EMAIL)
        if user is None or not user.is_bot:
            return self._create_and_onboard()
        self._maybe_reonboard(user)
        return user

    def _create_and_onboard(self) -> User:
        with self._db.write() as conn:
            prior = TableRead.get_user_by_email(conn, _EMAIL)
            if prior is not None:  # partial-create recovery
                user_id, acct, api_key = prior.user_id, prior.eth_key, prior.api_key
            else:
                user_id, acct, api_key = TableWrite.create_user(
                    conn,
                    email=_EMAIL,
                    password_hash=hash_password(_PASSWORD),
                    handle=None,
                )
        self._fund(acct)
        with self._db.write() as conn:
            TableWrite.mark_user_onboarded(conn, user_id)
            TableWrite.mark_user_as_bot(conn, api_key)
        with self._db.read() as conn:
            user = TableRead.get_user_by_userid(conn, user_id)
        assert user is not None
        log.info("house account %s provisioned", _EMAIL)
        return user

    def _approvals_need_wei(self, address: str) -> int:
        """What the house's three approvals cost: each one's estimate plus the
        pad `send_user_tx` adds, at the current price they go out with."""
        gas = sum(
            self._onchain.estimate_user_gas(fn, address) * (100 + GAS_BUFFER_PCT) // 100
            for fn in self._onchain.approval_calls()
        )
        return gas * self._onchain.gas_price()

    def _fund(self, acct) -> None:
        """Mint the collateral, fund exactly the approvals' gas, send the
        approvals. The house sends nothing after them: fills are the admin's
        matchOrders."""
        timeout = self._settings.tx_confirmations_timeout_s
        self._onchain.mint_to(
            acct.address, self._settings.house_mint_raw, timeout=timeout
        )
        self._onchain.fund_gas(
            acct.address, self._approvals_need_wei(acct.address), timeout=timeout
        )
        self._onchain.grant_user_approvals(acct, timeout=timeout)

    def _maybe_reonboard(self, user: User) -> None:
        """Repair an account the chain forgot: a wipe, not ordinary spending.

        Gated on `simulated_chain` and on the chain id (`is_disposable_chain`) for
        the same reason as the user-facing path: only a disposable chain can
        forget an account. The nonce is the signal, as for users: exact funding
        leaves the house's balance near zero, while its approvals leave a nonce
        of 3.
        """
        if not self._settings.simulated_chain or not is_disposable_chain(
            self._onchain.chain_id
        ):
            return
        try:
            if self._onchain.transaction_count(user.eth_address) > 0:
                return
        except Exception as exc:
            log.warning("chain nonce check failed for %s: %s", user.user_id, exc)
            return
        log.info("house account %s unknown to the chain (reset), re-onboarding", user.email)
        try:
            self._fund(user.eth_key)
        except Exception:
            log.exception("re-onboarding house account %s failed", user.email)
