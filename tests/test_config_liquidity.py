from agentpit.config import Settings


def test_liquidity_defaults():
    s = Settings()
    assert s.liquidity_engine_enabled is False
    assert s.mirror_assets_per_connection == 120
    assert abs(s.mirror_watchdog_seconds - 5.0) < 1e-9
    assert s.mirror_tape_enabled is True
    assert abs(s.mirror_target_refresh_seconds - 15.0) < 1e-9
    assert s.paper_balance_target_raw == 100_000_000_000  # $100k, 6dp
    assert s.house_mint_raw == 10**24  # 1e18 apUSD
    assert s.topup_cooldown_seconds == 86_400


def test_liquidity_env_override(monkeypatch):
    monkeypatch.setenv("LIQUIDITY_ENGINE", "true")
    assert Settings().liquidity_engine_enabled is True
