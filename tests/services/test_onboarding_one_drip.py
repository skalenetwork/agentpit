"""One account gets one collateral grant, however onboarding fails and is retried.

Nothing on chain stops a second grant (`Faucet.drip` mints on every call, the
token has no cap), so `AuthService` must drip once: a failure after the drip
releases the claim, and the retry must find the grant there. Each case uses ONE
chain object for both attempts, because the grant lives on the chain.
"""

import pytest
from requests import ReadTimeout
from web3.datastructures import AttributeDict
from web3.exceptions import TimeExhausted

from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import AdminGasPausedError, GasTopUpTimeoutError, InsufficientGasError, OnboardingError
from agentpit.services.auth_service import AuthService
from tests.onboarding_fakes import APPROVAL_GAS, GAS_PRICE, ONBOARDING_NEED, OnboardingChain, onboarding

#: What a failed attempt may raise: every documented way onboarding refuses.
_DOCUMENTED = (OnboardingError, GasTopUpTimeoutError, AdminGasPausedError, InsufficientGasError)
_ATTEMPTS = 5


class _FlakyChain(OnboardingChain):
    """Fails once, at `fault` ("fund_gas" or an approval's name) in the way
    `how` says, and otherwise behaves. The wallet keeps a native balance
    (credited by a top-up, debited by what a mined send burns), so the sponsor
    sizes a retry as the real one does and a late top-up is really there."""

    def __init__(self, fault: str, how: str) -> None:
        super().__init__()
        self._fault, self._how = fault, how
        self.native: dict[str, int] = {}
        self.approved: set[str] = set()  # approvals that mined with status 1

    def native_balance(self, address):
        return self.native.get(address.lower(), 0)

    def _fails_now(self, where: str) -> bool:
        failing = self._fault == where
        if failing:
            self._fault = ""  # fails once
        return failing

    def fund_gas(self, user_address, value_wei, *, timeout=30):
        failing = self._fails_now("fund_gas")
        if failing and self._how == "paused":
            raise AdminGasPausedError()
        receipt = super().fund_gas(user_address, value_wei, timeout=timeout)
        key = user_address.lower()
        self.native[key] = self.native.get(key, 0) + value_wei
        if failing:  # the top-up mined, but its receipt was not seen in time
            raise TimeExhausted("no receipt for the top-up")
        return receipt

    def send_as_user(self, user_account, fn, *, gas, max_fee, timeout=30, on_signed=None):
        failing = self._fails_now(fn)
        if failing and self._how == "read_timeout":
            raise ReadTimeout("no answer to the broadcast")
        if failing and self._how == "cannot_pay":
            raise InsufficientGasError("this account's wallet cannot pay")
        receipt = super().send_as_user(user_account, fn, gas=gas, max_fee=max_fee, timeout=timeout)
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
    o = onboarding(chain)

    failures = 0
    for _ in range(_ATTEMPTS):
        try:
            o.onboard()
            break
        except _DOCUMENTED:
            failures += 1
            assert o.claimed_at() is None  # a failed attempt hands the claim back: the retry must not wait
    assert failures == 1, "the fault fires once, so exactly one attempt fails"

    # The collateral: dripped once, so the wallet holds the grant, not two.
    assert chain.calls.count("faucet_drip") == 1
    assert chain.minted[o.acct.address.lower()] == 1
    assert chain.usd_balance(o.acct.address) == chain.signup_grant_raw
    # A top-up that landed late is not paid in full a second time.
    assert sum(wei for _, wei in chain.funded) <= 2 * ONBOARDING_NEED
    # And the account ends onboarded, all three approvals mined.
    assert chain.approved == set(_APPROVALS)
    assert o.user().onboarded_at is not None
    with o.db.read() as conn:
        assert TableRead.get_total_deposited(conn, o.user_id, 0) == chain.signup_grant_raw


def test_a_database_error_marking_the_row_onboarded_does_not_drip_again(monkeypatch):
    # The claim is not released on this failure (the onboarding path does not
    # catch the error), so the retry is a stale-claim takeover.
    o = onboarding(OnboardingChain())
    real = TableWrite.mark_user_onboarded
    failed: list[str] = []

    def flaky(conn, uid):
        if not failed:
            failed.append(uid)
            raise RuntimeError("database hiccup")
        return real(conn, uid)

    monkeypatch.setattr(TableWrite, "mark_user_onboarded", staticmethod(flaky))

    with pytest.raises(RuntimeError, match="hiccup"):
        o.onboard()
    o.set_claim(AuthService.ONBOARDING_CLAIM_STALE_S + 1)

    assert o.onboard().onboarded_at is not None
    assert o.chain.calls.count("faucet_drip") == 1
    assert o.chain.usd_balance(o.acct.address) == o.chain.signup_grant_raw


def test_a_wiped_account_is_dripped_again():
    # The guard is a balance check, not a once-ever flag: a chain wipe leaves
    # the account holding nothing, and that account is owed its grant.
    o = onboarding(OnboardingChain())
    o.onboard()
    assert o.chain.usd_balance(o.acct.address) == o.chain.signup_grant_raw

    o.chain.minted.clear()  # the chain forgot every balance
    o.service._run_onboarding(o.user())

    assert o.chain.calls.count("faucet_drip") == 2
    assert o.chain.usd_balance(o.acct.address) == o.chain.signup_grant_raw


def test_an_account_already_holding_the_grant_is_not_dripped():
    o = onboarding(OnboardingChain())
    o.chain.minted[o.acct.address.lower()] = 1  # the grant is already there

    o.onboard()

    assert "faucet_drip" not in o.chain.calls
    assert o.chain.calls.count("send_as_user") == 3
