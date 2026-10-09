"""The knobs of the user gas sponsor (spec 2026-10-08): every user-signed
transaction is topped up to exactly what it needs, so they bound that, not a grant."""

import pytest
from pydantic import ValidationError

from agentpit.config import Settings

# (env var, `Settings` attribute, the owner's default, an env override, as parsed)
_KNOBS = [
    ("AGENTPIT_SPONSOR_USER_GAS", "sponsor_user_gas", True, "false", False),
    ("AGENTPIT_MAX_TOPUP_GAS", "max_topup_gas", 1_000_000, "250000", 250_000),
    ("AGENTPIT_MIN_CLAIM_MICRO", "min_claim_micro", 10_000, "1", 1),  # default $0.01
    ("AGENTPIT_AUTO_REDEEM_MAX_PER_PASS", "auto_redeem_max_per_pass", 20, "1", 1),
]


def _settings(monkeypatch, **env):
    for var, *_ in _KNOBS:
        monkeypatch.delenv(var, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)


@pytest.mark.parametrize(("var", "attr", "default", "raw", "parsed"), _KNOBS)
def test_defaults_are_the_owner_decisions_and_env_overrides_them(
    monkeypatch, var, attr, default, raw, parsed
):
    got = getattr(_settings(monkeypatch), attr)
    assert (type(got), got) == (type(default), default)
    got = getattr(_settings(monkeypatch, **{var: raw}), attr)
    assert (type(got), got) == (type(parsed), parsed)


@pytest.mark.parametrize(
    "name, value",
    [
        ("AGENTPIT_MAX_TOPUP_GAS", "-1"),
        # A ceiling of 0 makes every top-up a RuntimeError, so every signup 500s.
        ("AGENTPIT_MAX_TOPUP_GAS", "0"),
        ("AGENTPIT_MIN_CLAIM_MICRO", "-1"),
        # With 0 `payout < minimum` never holds: worthless claims cost the admin gas.
        ("AGENTPIT_MIN_CLAIM_MICRO", "0"),
        # A pass that may send no claim at all would never redeem anything.
        ("AGENTPIT_AUTO_REDEEM_MAX_PER_PASS", "0"),
    ],
)
def test_out_of_range_values_are_refused_at_startup(monkeypatch, name, value):
    with pytest.raises(ValidationError):
        _settings(monkeypatch, **{name: value})
