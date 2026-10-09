import logging
import threading
import time

import pytest
from web3.exceptions import TimeExhausted, Web3RPCError

from agentpit.config import Settings
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import (
    AdminGasPausedError,
    GasPriceMovedError,
    GasTopUpTimeoutError,
    InsufficientGasError,
    OnboardingError,
)
from agentpit.onchain.tx_sender import TRANSFER_GAS
from agentpit.services.auth_service import AuthService
from tests.onboarding_fakes import APPROVAL_GAS, APPROVAL_LIMIT, GAS_PRICE, ONBOARDING_NEED, OnboardingChain, onboarding

STALE_S = AuthService.ONBOARDING_CLAIM_STALE_S + 1
_PRICE_MOVED = Web3RPCError(repr({"code": -32000, "message": "Transaction gas price lower than current eth_gasPrice"}))
_CANNOT_PAY = InsufficientGasError("this account's wallet cannot pay for the transaction")


class _Chain(OnboardingChain):
    """The wallet's top-up can fail (`fail`) or wait on a gate; every approval can be refused (`refuse`)."""

    def __init__(self, fail=None, gate: threading.Event | None = None, refuse=None):
        super().__init__()
        self._fail, self._gate, self._refuse = fail, gate, refuse

    def fund_gas(self, user_address, value_wei, *, timeout=30):
        if self._gate is not None:
            self._gate.wait(5)
        if self._fail is not None:
            raise self._fail
        return super().fund_gas(user_address, value_wei, timeout=timeout)

    def send_as_user(self, *args, **kwargs):
        if self._refuse is not None:
            self.calls.append("send_as_user")
            raise self._refuse
        return super().send_as_user(*args, **kwargs)


def test_onboards_once_and_a_repeat_is_free():
    o = onboarding(_Chain())
    assert o.onboard().onboarded_at is not None
    assert o.onboard().onboarded_at is not None
    assert len(o.chain.funded) == 1


def test_a_held_claim_refuses_a_second_onboarding():
    o = onboarding(_Chain())
    o.set_claim(0)
    with pytest.raises(OnboardingError, match="already being set up"):
        o.onboard()
    assert o.chain.funded == []


def test_a_stale_claim_is_taken_over():
    o = onboarding(_Chain())
    o.set_claim(STALE_S)
    o.onboard()
    assert len(o.chain.funded) == 1


# Every refusal reaches the caller as itself -- a 503 "the platform is busy" or
# "the network fee rose", a 402 -- not wrapped as a 400 `OnboardingError` and
# logged with a traceback per sign-in; only an unknown failure is.
@pytest.mark.parametrize(
    ("chain", "raised"),
    [
        pytest.param(_Chain(fail=RuntimeError("chain down")), OnboardingError, id="chain-down"),
        pytest.param(_Chain(fail=AdminGasPausedError()), AdminGasPausedError, id="breaker-paused"),
        pytest.param(_Chain(fail=TimeExhausted("no receipt for the top-up")), GasTopUpTimeoutError, id="top-up-timeout"),
        # The fee keeps rising: the node refuses every approval, the re-sized retry included.
        pytest.param(_Chain(refuse=_PRICE_MOVED), GasPriceMovedError, id="gas-price-moved"),
        # The sponsor's last word on a wallet the node keeps refusing.
        pytest.param(_Chain(refuse=_CANNOT_PAY), InsufficientGasError, id="wallet-cannot-pay-402"),
    ],
)
def test_a_refused_onboarding_surfaces_its_error_and_releases_the_claim(chain, raised, caplog):
    o = onboarding(chain)
    with caplog.at_level(logging.ERROR, logger="agentpit.services.auth_service"):
        with pytest.raises(raised):
            o.onboard()

    # A refused top-up signs nothing from a wallet that was never funded.
    assert ("send_as_user" in chain.calls) == (chain._refuse is not None)
    if raised is not OnboardingError:
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert o.claimed_at() is None
    good = _Chain()
    o.service._onchain = good  # the honest retry a second later
    o.onboard()
    assert len(good.funded) == 1


def test_parallel_first_sign_ins_fund_once():
    gate = threading.Event()
    o = onboarding(_Chain(gate=gate))
    errors: list[Exception] = []

    def run():
        try:
            o.onboard()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    time.sleep(0.3)
    gate.set()
    for t in threads:
        t.join(10)
    assert len(o.chain.funded) == 1
    assert len(errors) == 1 and isinstance(errors[0], OnboardingError)


def test_clear_user_onboarded_also_clears_the_claim():
    o = onboarding(_Chain())
    o.onboard()
    with o.db.write() as conn:
        TableWrite.clear_user_onboarded(conn, o.user_id)
    assert o.claimed_at() is None


# The user-gas switch stops claims, splits and merges from being funded, never
# signups: without the top-up no account or agent could be created.
@pytest.mark.parametrize(
    "env", [{}, {"AGENTPIT_SPONSOR_USER_GAS": False}], ids=["sponsored", "user-gas-switch-off"]
)
def test_onboarding_tops_up_exactly_the_approvals_and_signs_them_as_the_user(env):
    o = onboarding(_Chain(), Settings(**env))
    assert o.onboard().onboarded_at is not None
    # The drip, one top-up to exactly three approvals' limits, the approvals.
    assert o.chain.calls == ["faucet_drip", "fund_gas"] + ["send_as_user"] * 3
    assert o.chain.funded == [(o.acct.address, ONBOARDING_NEED)]
    assert o.chain.sent == [(o.acct.address, fn, APPROVAL_LIMIT, GAS_PRICE) for fn in o.chain.approval_calls()]


def test_onboarding_gas_is_booked_to_the_account():
    o = onboarding(_Chain())
    day = int(time.time()) // 86_400
    user = o.onboard()
    with o.db.read() as conn:
        assert TableRead.sponsored_gas_used(conn, user.api_key, day) == TRANSFER_GAS + 3 * APPROVAL_GAS


def test_a_transaction_in_progress_reads_as_a_lost_claim():
    o = onboarding(_Chain())
    with o.hold_lock():
        with pytest.raises(OnboardingError, match="already being set up"):
            o.onboard()
    assert o.chain.calls == []  # refused before the admin sent anything
    assert o.claimed_at() is None
    assert o.onboard().onboarded_at is not None
