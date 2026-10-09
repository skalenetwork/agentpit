from agentpit.config import Settings

_VARS = (
    "AGENTPIT_MIN_ORDER_NOTIONAL_MICRO",
    "AGENTPIT_MAX_LIVE_ORDERS_PER_ACCOUNT",
    "AGENTPIT_MAX_MAKERS_PER_MATCH",
    "AGENTPIT_DAILY_SPONSORED_GAS_PER_ACCOUNT",
    "AGENTPIT_ADMIN_GAS_ALARM_GAS",
    "AGENTPIT_ADMIN_GAS_STOP_GAS",
    "AGENTPIT_ADMIN_GAS_CHECK_INTERVAL_SECONDS",
)


def _settings(monkeypatch, **env):
    for name in _VARS:
        monkeypatch.delenv(name, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)


def test_defaults_are_the_owner_decisions(monkeypatch):
    s = _settings(monkeypatch)
    assert s.min_order_notional_micro == 1_000_000
    assert s.max_live_orders_per_account == 200
    assert s.max_makers_per_match == 20
    assert s.daily_sponsored_gas_per_account == 20_000_000
    assert s.admin_gas_alarm_gas == 420_000_000
    assert s.admin_gas_stop_gas == 105_000_000
    assert s.admin_gas_check_interval_seconds == 60.0


def test_env_overrides(monkeypatch):
    s = _settings(monkeypatch, AGENTPIT_MIN_ORDER_NOTIONAL_MICRO="0", AGENTPIT_ADMIN_GAS_STOP_GAS="7")
    assert s.min_order_notional_micro == 0
    assert s.admin_gas_stop_gas == 7


def test_the_signup_gas_grant_is_gone_and_an_old_env_file_still_loads(
    tmp_path, monkeypatch
):
    """Users get exact per-transaction top-ups now, so the grant setting was
    deleted. A deployment's env file written before that still names it, and
    that file must load rather than fail on an unknown key."""
    monkeypatch.delenv("AGENTPIT_SIGNUP_GAS_GRANT_WEI", raising=False)
    env = tmp_path / ".env"
    env.write_text("AGENTPIT_SIGNUP_GAS_GRANT_WEI=20000000000000000\n")

    s = Settings(_env_file=str(env))

    assert "signup_gas_grant_wei" not in Settings.model_fields
    assert not hasattr(s, "signup_gas_grant_wei")
