"""The API's own pool carries the configured timeouts; its startup migration
runs without one."""

from fastapi.testclient import TestClient

from agentpit.api import app as app_module
from agentpit.api.app import create_app
from agentpit.api.deps import get_db_session


def _show(conn, name):
    return conn.execute(f"SHOW {name}").fetchone()[name]


def test_the_api_pool_uses_the_configured_timeouts(monkeypatch):
    monkeypatch.setenv("AGENTPIT_DB_STATEMENT_TIMEOUT_MS", "1234")
    monkeypatch.setenv("AGENTPIT_DB_IDLE_IN_TRANSACTION_TIMEOUT_MS", "60000")
    app = create_app()
    db = app.dependency_overrides[get_db_session]()
    with db.read() as conn:
        assert _show(conn, "statement_timeout") == "1234ms"
        assert _show(conn, "idle_in_transaction_session_timeout") == "1min"


def test_startup_migration_runs_without_a_timeout(monkeypatch):
    monkeypatch.setenv("AGENTPIT_DB_STATEMENT_TIMEOUT_MS", "1234")
    seen = []
    real = app_module.TableCreate.create_all_tables

    def spy(conn):
        seen.append(_show(conn, "statement_timeout"))
        real(conn)

    monkeypatch.setattr(app_module.TableCreate, "create_all_tables", spy)
    app = create_app()
    with TestClient(app):
        pass
    assert seen == ["0"]
