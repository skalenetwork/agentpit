"""`_maybe_reonboard`: what reads as a wiped chain, and who never gets the repair.

The signal is the wallet's nonce: onboarding sends three approvals from it, so
an onboarded account the chain never saw send is one the chain forgot (the
native balance stopped being a signal once exact top-ups made a near-empty
wallet normal). Accounts that exported their key (KEY_EXPORTED_AT) never get
the repair. Each refusal is paired with the repair on the same wiped chain
without its gate, so it fails if the gate goes away.
"""

import logging
import threading
from contextlib import nullcontext

import pytest

from agentpit.config import Settings
from tests.onboarding_fakes import SKALE_BASE_TESTNET, OnboardingChain, onboarding


class _WipedChain(OnboardingChain):
    """Every wallet reads empty: a chain reset, or with a nonce above 0 an ordinary day of exact top-ups."""

    deployment_id = "test-deployment"

    def native_balance(self, *_args, **_kwargs):
        self.calls.append("native_balance")
        return 0

    def usd_balance(self, *_args, **_kwargs):
        self.calls.append("usd_balance")
        return 0


def _reonboard(*, exported_at=None, nonce=0, hold_lock=False, chain_id=31337) -> _WipedChain:
    """Log in an onboarded account; `SIMULATED_CHAIN` is on whatever the chain."""
    chain = _WipedChain(nonce=nonce, chain_id=chain_id)
    o = onboarding(chain, Settings(AGENTPIT_SIMULATED_CHAIN=True))
    user = o.mark_onboarded(exported_at)
    with o.hold_lock() if hold_lock else nullcontext():
        o.service._maybe_reonboard(user)
    return chain


def test_a_wiped_wallet_only_we_hold_is_reonboarded():
    chain = _reonboard()
    assert len(chain.funded) == 1 and chain.calls.count("send_as_user") == 3


@pytest.mark.parametrize(
    ("kwargs", "calls"),
    [
        # The nonce is read and nothing else: no drip, no top-up, no approvals.
        pytest.param({"nonce": 3}, ["transaction_count"], id="has-sent-however-empty"),
        pytest.param({"exported_at": 1_700_000_000}, [], id="key-exported"),
        pytest.param({"hold_lock": True}, ["transaction_count"], id="transaction-in-progress"),
        pytest.param({"chain_id": SKALE_BASE_TESTNET}, [], id="durable-chain-even-if-simulated"),
    ],
)
def test_the_repair_is_skipped(kwargs, calls, caplog):
    with caplog.at_level(logging.INFO, logger="agentpit.services.auth_service"):
        assert _reonboard(**kwargs).calls == calls
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]  # skipped, no traceback


class _StaleNonce(_WipedChain):
    """The first nonce read returns 0 and then waits: its caller holds a stale
    zero while another sign-in re-onboards the same wallet (the nonce goes to 3
    as the approvals mine)."""

    def __init__(self) -> None:
        super().__init__(nonce=0)
        self.holding = threading.Event()  # the first reader has its zero
        self.release = threading.Event()

    def transaction_count(self, address):
        value = super().transaction_count(address)
        if not self.holding.is_set():  # the first read only: the other sign-in starts once it is set
            self.holding.set()
            assert self.release.wait(10), "the second sign-in never finished"
        return value


def test_a_late_reonboard_does_not_drip_or_approve_a_second_time():
    # Two sign-ins both read a zero nonce and the slower one takes the lock only
    # after the faster has finished: its zero is stale, and repeating the drip
    # and the approvals would hand the account a second grant.
    chain = _StaleNonce()
    o = onboarding(chain, Settings(AGENTPIT_SIMULATED_CHAIN=True))
    user = o.mark_onboarded()

    slow = threading.Thread(target=o.service._maybe_reonboard, args=(user,))
    slow.start()
    try:
        assert chain.holding.wait(10), "the first sign-in never read the nonce"
        o.service._maybe_reonboard(user)  # the fast one runs to completion
        assert chain._nonce == 3
    finally:
        chain.release.set()
        slow.join(10)
    assert not slow.is_alive()

    assert chain.calls.count("faucet_drip") == 1
    assert chain.calls.count("send_as_user") == 3
