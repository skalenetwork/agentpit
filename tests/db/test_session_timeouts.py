"""Per-connection timeouts on DbSession: opt-in, and never applied to schema
migrations."""

import psycopg.errors
import pytest

from agentpit.config import Settings
from agentpit.db import session as session_module
from agentpit.db.session import DbSession


def _show(conn, name: str) -> str:
    return conn.execute(f"SHOW {name}").fetchone()[name]


def test_no_timeouts_unless_asked():
    db = DbSession(Settings().database_url, create_tables=False)
    with db.read() as conn:
        assert _show(conn, "statement_timeout") == "0"
        assert _show(conn, "idle_in_transaction_session_timeout") == "0"


def test_timeouts_apply_to_every_pooled_connection():
    db = DbSession(
        Settings().database_url,
        create_tables=False,
        statement_timeout_ms=1500,
        idle_in_transaction_timeout_ms=60000,
    )
    with db.read() as a, db.write() as b:
        for conn in (a, b):
            assert _show(conn, "statement_timeout") == "1500ms"
            assert _show(conn, "idle_in_transaction_session_timeout") == "1min"
            assert _show(conn, "jit") == "off"


def test_a_statement_over_the_timeout_is_cancelled():
    db = DbSession(Settings().database_url, create_tables=False, statement_timeout_ms=200)
    with pytest.raises(psycopg.errors.QueryCanceled):
        with db.read() as conn:
            conn.execute("SELECT pg_sleep(1)")


def test_table_creation_runs_without_a_timeout_and_the_pool_keeps_it(monkeypatch):
    seen = []
    real = session_module.TableCreate.create_all_tables

    def spy(conn):
        seen.append(_show(conn, "statement_timeout"))
        real(conn)

    monkeypatch.setattr(session_module.TableCreate, "create_all_tables", spy)
    db = DbSession(Settings().database_url, min_size=1, max_size=1, statement_timeout_ms=1500)
    assert seen == ["0"]
    with db.read() as conn:  # max_size=1: the very connection that migrated
        assert _show(conn, "statement_timeout") == "1500ms"
