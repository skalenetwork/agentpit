import pytest
from pydantic import ValidationError

from agentpit.config import Settings


def _settings(monkeypatch, **env):
    monkeypatch.delenv("AGENTPIT_ADMIN_TX_MAX_IN_FLIGHT", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)


def test_admin_tx_max_in_flight_defaults_to_64(monkeypatch):
    assert _settings(monkeypatch).admin_tx_max_in_flight == 64


def test_admin_tx_max_in_flight_is_configurable(monkeypatch):
    s = _settings(monkeypatch, AGENTPIT_ADMIN_TX_MAX_IN_FLIGHT="1")
    assert s.admin_tx_max_in_flight == 1


def test_admin_tx_max_in_flight_rejects_zero(monkeypatch):
    with pytest.raises(ValidationError):
        _settings(monkeypatch, AGENTPIT_ADMIN_TX_MAX_IN_FLIGHT="0")
