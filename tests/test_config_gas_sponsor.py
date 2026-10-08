"""The knobs of the user gas sponsor (spec 2026-10-08, gasless user
transactions): every user-signed transaction is topped up to exactly what it
needs, so these bound that, not a grant."""

import pytest
from pydantic import ValidationError

from agentpit.config import Settings

_VARS = (
    "AGENTPIT_SPONSOR_USER_GAS",
    "AGENTPIT_MAX_TOPUP_GAS",
    "AGENTPIT_MIN_CLAIM_MICRO",
    "AGENTPIT_AUTO_REDEEM_MAX_PER_PASS",
)


def _settings(monkeypatch, **env):
    for name in _VARS:
        monkeypatch.delenv(name, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)


def test_defaults_are_the_owner_decisions(monkeypatch):
    s = _settings(monkeypatch)
    assert s.sponsor_user_gas is True
    assert s.max_topup_gas == 1_000_000
    assert s.min_claim_micro == 10_000  # $0.01
    assert s.auto_redeem_max_per_pass == 20


def test_env_overrides(monkeypatch):
    s = _settings(
        monkeypatch,
        AGENTPIT_SPONSOR_USER_GAS="false",
        AGENTPIT_MAX_TOPUP_GAS="250000",
        AGENTPIT_MIN_CLAIM_MICRO="1",
        AGENTPIT_AUTO_REDEEM_MAX_PER_PASS="1",
    )
    assert s.sponsor_user_gas is False
    assert s.max_topup_gas == 250_000
    assert s.min_claim_micro == 1
    assert s.auto_redeem_max_per_pass == 1


@pytest.mark.parametrize(
    "name, value",
    [
        ("AGENTPIT_MAX_TOPUP_GAS", "-1"),
        # A ceiling of 0 turns every top-up into a RuntimeError, onboarding's
        # included, so every signup would 500.
        ("AGENTPIT_MAX_TOPUP_GAS", "0"),
        ("AGENTPIT_MIN_CLAIM_MICRO", "-1"),
        # With a minimum of 0, `payout < minimum` is never true: a position
        # worth nothing would be claimed for, at the admin's expense.
        ("AGENTPIT_MIN_CLAIM_MICRO", "0"),
        # A pass that may send no claim at all would never redeem anything.
        ("AGENTPIT_AUTO_REDEEM_MAX_PER_PASS", "0"),
    ],
)
def test_out_of_range_values_are_refused_at_startup(monkeypatch, name, value):
    with pytest.raises(ValidationError):
        _settings(monkeypatch, **{name: value})
