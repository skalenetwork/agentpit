"""Which chains count as disposable. Login re-onboarding on one is in tests/services/test_reonboard.py."""

import logging

from agentpit.api.app import _warn_if_simulated_on_durable_chain
from agentpit.config import Settings
from agentpit.onchain.deployment import is_disposable_chain
from tests.onboarding_fakes import SKALE_BASE_TESTNET


def test_only_anvil_is_disposable():
    assert is_disposable_chain(31337)
    assert not is_disposable_chain(SKALE_BASE_TESTNET)
    assert not is_disposable_chain(1187947933)


def test_startup_shouts_when_simulated_is_set_on_a_durable_chain(caplog):
    with caplog.at_level(logging.ERROR, logger="agentpit.api.app"):
        _warn_if_simulated_on_durable_chain(Settings(AGENTPIT_SIMULATED_CHAIN=True), SKALE_BASE_TESTNET)
        _warn_if_simulated_on_durable_chain(Settings(AGENTPIT_SIMULATED_CHAIN=True), 31337)
        _warn_if_simulated_on_durable_chain(Settings(AGENTPIT_SIMULATED_CHAIN=False), SKALE_BASE_TESTNET)
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and "AGENTPIT_SIMULATED_CHAIN" in errors[0].getMessage()
