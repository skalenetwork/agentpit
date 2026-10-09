from agentpit.config import Settings


def _settings(monkeypatch, **env):
    for k in ("SYNC", "AGENTPIT_SYNC_MIN_VOLUME_24H", "AGENTPIT_SYNC_EXCLUDE_CHURN_SERIES"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)


def test_new_knobs_defaults(monkeypatch):
    assert _settings(monkeypatch).sync_min_volume_24h == 1_000.0
    assert _settings(monkeypatch, AGENTPIT_SYNC_MIN_VOLUME_24H="2500").sync_min_volume_24h == 2_500.0


def test_churn_exclusion_is_on_by_default_and_reversible(monkeypatch):
    """The daily-temperature + sports-prop series are 89% of new creations, so
    the default is to drop them; the flag is there to switch it back without a
    code change."""
    assert _settings(monkeypatch).sync_exclude_churn_series is True
    off = _settings(monkeypatch, AGENTPIT_SYNC_EXCLUDE_CHURN_SERIES="false")
    assert off.sync_exclude_churn_series is False
