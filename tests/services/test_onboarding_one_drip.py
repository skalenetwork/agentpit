"""One account gets one collateral grant, however onboarding fails and is retried.

`Faucet.drip` mints its amount on every call and `AgentpitUSD.mint` has no cap,
so nothing on chain stops a second grant: it is `AuthService` that has to drip
an account once. Onboarding drips first and then tops up the wallet and sends
the three approvals, so every failure after the drip -- the top-up timing out,
the breaker pausing, an approval reverting or losing its answer, a database
error marking the row onboarded -- releases the claim, and the retry has to
find the grant already there.

Every case uses ONE chain object for both attempts, because the grant lives on
the chain: a fresh fake per attempt (as `test_failed_onboarding_releases_the_
claim` does) forgets it.
"""

import pytest
from requests import ReadTimeout
from web3.datastructures import AttributeDict
from web3.exceptions import TimeExhausted

from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import (
    AdminGasPausedError,
    GasTopUpTimeoutError,
    InsufficientGasError,
    OnboardingError,
)
from agentpit.services.auth_service import AuthService
from tests.onboarding_fakes import (
    APPROVAL_GAS,
    GAS_PRICE,
    ONBOARDING_NEED,
    OnboardingChain,
)
from tests.services.test_onboarding_claim import _claimed_at, _row, _service

#: What a failed attempt may raise: every documented way onboarding refuses.
_DOCUMENTED = (
    OnboardingError,
    GasTopUpTimeoutError,
    AdminGasPausedError,
    InsufficientGasError,
)
_ATTEMPTS = 5


class _FlakyChain(OnboardingChain):
    """Fails once, at the point it is told to, and otherwise behaves.

    `fault` is "fund_gas" or one of the approvals' names; `how` is what the
    failure looks like. The wallet keeps a native balance -- credited by a
    top-up, debited by what a mined send burns -- so the sponsor sizes a retry
    the way the real one does, and a top-up that "landed late" is really
    there when the retry looks.
    """

    def __init__(self, fault: str, how: str) -> None:
        super().__init__()
        self._fault = fault
        self._how = how
        self._armed = True
        self.native: dict[str, int] = {}
        #: Approvals that mined with status 1, by name.
        self.approved: set[str] = set()

    def native_balance(self, address):
        return self.native.get(address.lower(), 0)

    def _fails_now(self, where: str) -> bool:
        if self._armed and self._fault == where:
            self._armed = False
            return True
        return False

    def fund_gas(self, user_address, value_wei, *, timeout=30):
        failing = self._fails_now("fund_gas")
        if failing and self._how == "paused":
            raise AdminGasPausedError()
        receipt = super().fund_gas(user_address, value_wei, timeout=timeout)
        self.native[user_address.lower()] = (
            self.native.get(user_address.lower(), 0) + value_wei
        )
        if failing:
            # The top-up mined, but its receipt was not seen in time.
            raise TimeExhausted("no receipt for the top-up")
        return receipt

    def send_as_user(self, user_account, fn, *, gas, max_fee, timeout=30):
        failing = self._fails_now(fn)
        if failing and self._how == "read_timeout":
            raise ReadTimeout("no answer to the broadcast")
        if failing and self._how == "cannot_pay":
            raise InsufficientGasError("this account's wallet cannot pay")
        receipt = super().send_as_user(
            user_account, fn, gas=gas, max_fee=max_fee, timeout=timeout
        )
        key = user_account.address.lower()
        self.native[key] = self.native.get(key, 0) - APPROVAL_GAS * GAS_PRICE
        if failing:  # "reverted": mined, paid for, status 0
            return AttributeDict({**receipt, "status": 0})
        self.approved.add(fn)
        return receipt


_APPROVALS = OnboardingChain().approval_calls()

_FAULTS = [
    pytest.param("fund_gas", "late", id="top-up-mines-after-its-timeout"),
    pytest.param("fund_gas", "paused", id="breaker-paused"),
    *[
        pytest.param(approval, how, id=f"approval-{i + 1}-{how}")
        for i, approval in enumerate(_APPROVALS)
        for how in ("reverted", "read_timeout", "cannot_pay")
    ],
]


@pytest.mark.parametrize(("fault", "how"), _FAULTS)
def test_a_failed_onboarding_retried_on_the_same_chain_grants_once(fault, how):
    chain = _FlakyChain(fault, how)
    service, db = _service(chain)
    user_id, acct = _row(db, f"drip-{fault}-{how}".replace("(", "").replace(")", "") + "@example.com")

    failures = 0
    for _ in range(_ATTEMPTS):
        try:
            service._onboard_new_account(user_id, acct)
            break
        except _DOCUMENTED:
            failures += 1
            # A failed attempt hands the claim back: the retry must not wait.
            assert _claimed_at(db, user_id) is None
    assert failures == 1, "the fault fires once, so exactly one attempt fails"

    # The collateral: dripped once, so the wallet holds the grant, not two.
    assert chain.calls.count("faucet_drip") == 1
    assert chain.minted[acct.address.lower()] == 1
    assert chain.usd_balance(acct.address) == chain.signup_grant_raw
    # A top-up that landed late is not paid in full a second time.
    assert sum(wei for _, wei in chain.funded) <= 2 * ONBOARDING_NEED
    # And the account ends onboarded, all three approvals mined.
    assert chain.approved == set(_APPROVALS)
    with db.read() as conn:
        user = TableRead.get_user_by_userid(conn, user_id)
        assert user is not None and user.onboarded_at is not None
        assert TableRead.get_total_deposited(conn, user_id, 0) == chain.signup_grant_raw


def test_a_database_error_marking_the_row_onboarded_does_not_drip_again(monkeypatch):
    """The claim is not released on this failure (the error is not one the
    onboarding path catches), so the retry is a stale-claim takeover."""
    chain = OnboardingChain()
    service, db = _service(chain)
    user_id, acct = _row(db, "marked@example.com")
    real = TableWrite.mark_user_onboarded
    calls = {"n": 0}

    def flaky(conn, uid):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database hiccup")
        return real(conn, uid)

    monkeypatch.setattr(TableWrite, "mark_user_onboarded", staticmethod(flaky))

    with pytest.raises(RuntimeError, match="hiccup"):
        service._onboard_new_account(user_id, acct)
    with db.write() as conn:
        conn.execute(
            "UPDATE users SET ONBOARDING_STARTED_AT = ONBOARDING_STARTED_AT - %s "
            "WHERE USER_ID = %s",
            (AuthService.ONBOARDING_CLAIM_STALE_S + 1, user_id),
        )

    assert service._onboard_new_account(user_id, acct).onboarded_at is not None
    assert chain.calls.count("faucet_drip") == 1
    assert chain.usd_balance(acct.address) == chain.signup_grant_raw


def test_a_wiped_account_is_dripped_again():
    """The guard is a balance check, not a once-ever flag: a chain wipe leaves
    the account holding nothing, and that account is owed its grant."""
    chain = OnboardingChain()
    service, db = _service(chain)
    user_id, acct = _row(db, "wiped@example.com")
    service._onboard_new_account(user_id, acct)
    assert chain.usd_balance(acct.address) == chain.signup_grant_raw

    chain.minted.clear()  # the chain forgot every balance
    service._run_onboarding(_user(db, user_id))

    assert chain.calls.count("faucet_drip") == 2
    assert chain.usd_balance(acct.address) == chain.signup_grant_raw


def test_an_account_already_holding_the_grant_is_not_dripped():
    chain = OnboardingChain()
    service, db = _service(chain)
    user_id, acct = _row(db, "holding@example.com")
    chain.minted[acct.address.lower()] = 1  # the grant is already there

    service._onboard_new_account(user_id, acct)

    assert "faucet_drip" not in chain.calls
    assert chain.calls.count("send_as_user") == 3


def _user(db, user_id):
    with db.read() as conn:
        user = TableRead.get_user_by_userid(conn, user_id)
    assert user is not None
    return user
