import pytest
from pydantic import ValidationError

from agentpit.config import Settings

_VARS = ("AGENTPIT_DB_STATEMENT_TIMEOUT_MS", "AGENTPIT_DB_IDLE_IN_TRANSACTION_TIMEOUT_MS")


def _settings(monkeypatch, **env):
    for k in _VARS:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)


def test_defaults(monkeypatch):
    s = _settings(monkeypatch)
    assert s.db_statement_timeout_ms == 30_000
    assert s.db_idle_in_transaction_timeout_ms == 300_000


def test_zero_disables(monkeypatch):
    s = _settings(
        monkeypatch,
        AGENTPIT_DB_STATEMENT_TIMEOUT_MS="0",
        AGENTPIT_DB_IDLE_IN_TRANSACTION_TIMEOUT_MS="0",
    )
    assert s.db_statement_timeout_ms == 0
    assert s.db_idle_in_transaction_timeout_ms == 0


def test_negative_is_rejected(monkeypatch):
    with pytest.raises(ValidationError):
        _settings(monkeypatch, AGENTPIT_DB_STATEMENT_TIMEOUT_MS="-1")
