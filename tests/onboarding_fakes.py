"""Just enough `OnchainAdmin` for `AuthService` onboarding, and a harness over it.

`UserGasSponsor` prices the approvals, tops the wallet up and sends them signed
by the user's key, so a fake needs the surface it reads too. Test files subclass
`OnboardingChain` for the one thing they are about.
"""

import time
from contextlib import AbstractContextManager
from dataclasses import dataclass

from eth_account.signers.local import LocalAccount
from web3.datastructures import AttributeDict

from agentpit.auth.jwt import JwtCoder
from agentpit.config import Settings
from agentpit.datastructures.user import User
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.services.auth_service import AuthService
from agentpit.services.gas_sponsor import UserGasSponsor
from tests.db_helpers import fresh_test_db

SKALE_BASE_TESTNET = 324705682  # a durable chain: re-onboarding must never run on it
APPROVAL_GAS = 46_000  # what every fake approval estimates and mines at (real: ~46k)
GAS_PRICE = 1_000_000_000  # about anvil's eth_gasPrice
APPROVAL_LIMIT = APPROVAL_GAS * 120 // 100  # the sponsor sends each with the estimate plus 20%
ONBOARDING_NEED = 3 * APPROVAL_LIMIT * GAS_PRICE  # an empty wallet's whole top-up
SIGNUP_GRANT = 100_000  # what one faucet drip mints (`OnchainAdmin.signup_grant_raw`)


class OnboardingChain:
    """Records, in order, what onboarding did (`calls`), the top-ups it sent
    (`funded`, as (address, wei)) and each user-signed send (`sent`, as
    (signer address, call, gas limit, max fee)).

    Keeps the two facts onboarding reads back from a chain: the collateral
    each drip minted (`minted`, per lowercased address, in drips) and the
    wallet's nonce, which every mined user transaction advances. `chain_id` is
    anvil's unless told otherwise: re-onboarding only runs on a wipeable chain."""

    deployment_id = "0xctf"
    signup_grant_raw = SIGNUP_GRANT

    def __init__(self, *, nonce: int = 0, chain_id: int = 31337) -> None:
        self.calls: list[str] = []
        self.funded: list[tuple[str, int]] = []
        self.sent: list[tuple[str, object, int, int]] = []
        self.minted: dict[str, int] = {}
        self._nonce = nonce
        self.chain_id = chain_id

    def faucet_drip(self, recipient, *, timeout=30):
        self.calls.append("faucet_drip")
        key = recipient.lower()
        self.minted[key] = self.minted.get(key, 0) + 1
        return AttributeDict({"status": 1, "gasUsed": 50_000})

    def approval_calls(self):  # stand-ins: the sponsor only hands them to the two fakes below
        return ["approve(exchange)", "approve(ctf)", "setApprovalForAll(exchange)"]

    def transaction_count(self, address):
        self.calls.append("transaction_count")
        return self._nonce

    def usd_balance(self, address):
        return self.minted.get(address.lower(), 0) * self.signup_grant_raw

    def gas_price(self):
        return GAS_PRICE

    def estimate_user_gas(self, fn, address):
        return APPROVAL_GAS

    def native_balance(self, address):
        return 0

    def fund_gas(self, user_address, value_wei, *, timeout=30):
        self.calls.append("fund_gas")
        self.funded.append((user_address, value_wei))
        return AttributeDict({"status": 1, "gasUsed": 21_000})

    def send_as_user(self, user_account, fn, *, gas, max_fee, timeout=30, on_signed=None):
        self.calls.append("send_as_user")
        self.sent.append((user_account.address, fn, gas, max_fee))
        self._nonce += 1
        return AttributeDict({"status": 1, "gasUsed": APPROVAL_GAS})


@dataclass
class Onboarding:
    """A fresh, not yet onboarded user row and an `AuthService` over `chain`."""

    chain: OnboardingChain
    settings: Settings
    service: AuthService
    db: DbSession
    user_id: str
    acct: LocalAccount

    def onboard(self) -> User:
        return self.service._onboard_new_account(self.user_id, self.acct)

    def user(self) -> User:
        with self.db.read() as conn:
            user = TableRead.get_user_by_userid(conn, self.user_id)
        assert user is not None
        return user

    def claimed_at(self) -> int | None:
        with self.db.read() as conn:
            sql = "SELECT ONBOARDING_STARTED_AT AS S FROM users WHERE USER_ID = %s"
            return conn.execute(sql, (self.user_id,)).fetchone()["S"]

    def set_claim(self, age_s: int) -> None:
        """Take the onboarding claim, `age_s` seconds ago."""
        with self.db.write() as conn:
            sql = "UPDATE users SET ONBOARDING_STARTED_AT = %s WHERE USER_ID = %s"
            conn.execute(sql, (int(time.time()) - age_s, self.user_id))

    def mark_onboarded(self, exported_at: int | None = None) -> User:
        """As a signup that finished. `exported_at` sets KEY_EXPORTED_AT: nothing
        writes it any more, only rows from before the export routes went have it."""
        with self.db.write() as conn:
            TableWrite.mark_user_onboarded(conn, self.user_id)
            if exported_at is not None:
                sql = "UPDATE users SET KEY_EXPORTED_AT = %s WHERE USER_ID = %s"
                conn.execute(sql, (exported_at, self.user_id))
        return self.user()

    def hold_lock(self) -> AbstractContextManager:
        """Another request mid-send for this account."""
        return UserGasSponsor(self.db, self.chain, self.settings).locked(self.user())  # type: ignore[arg-type]


def onboarding(chain: OnboardingChain, settings: Settings | None = None) -> Onboarding:
    db, settings = fresh_test_db(), settings or Settings()
    service = AuthService(db, JwtCoder(settings), chain, settings)  # type: ignore[arg-type]
    with db.write() as conn:
        user_id, acct, _ = TableWrite.create_user(conn, email="user@example.com", password_hash=None, handle=None)
    return Onboarding(chain, settings, service, db, user_id, acct)
