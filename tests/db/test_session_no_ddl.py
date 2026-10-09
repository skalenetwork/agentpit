"""`DbSession(create_tables=False)` must open a usable pool without DDL.

The one-off backfills in `scripts/` connect to a database the API is already
serving. Re-running the idempotent DDL from that second process is not free —
every `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` takes an AccessExclusiveLock
even when the column already exists — and on production it deadlocked against
the live API mid-backfill:

    psycopg.errors.DeadlockDetected: deadlock detected
    DETAIL: Process A waits for AccessExclusiveLock on relation (markets);
    blocked by process B. Process B waits for AccessShareLock on a relation
    A already holds.
"""

from __future__ import annotations

from unittest.mock import patch

from agentpit.db.session import DbSession
from tests.db_helpers import TEST_DSN, fresh_test_db


def test_create_tables_false_runs_no_ddl():
    fresh_test_db().close()  # schema exists before we open the silent session
    with patch("agentpit.db.session.TableCreate.create_all_tables") as ddl:
        db = DbSession(TEST_DSN, min_size=0, max_idle=5.0, create_tables=False)
        try:
            assert ddl.call_count == 0
        finally:
            db.close()


def test_the_silent_session_can_still_read_and_write():
    """Skipping the DDL must not skip anything else — the pool is the point."""
    fresh_test_db().close()
    db = DbSession(TEST_DSN, min_size=0, max_idle=5.0, create_tables=False)
    try:
        with db.write() as conn:
            conn.execute(
                "INSERT INTO trades (TRADE_ID, ASSET_ID, MATCH_KIND) "
                "VALUES ('no-ddl-1', 'tok', 'NORMAL')"
            )
        with db.read() as conn:
            row = conn.execute(
                "SELECT MATCH_KIND FROM trades WHERE TRADE_ID = 'no-ddl-1'"
            ).fetchone()
        assert row is not None and row["MATCH_KIND"] == "NORMAL"
    finally:
        db.close()


def test_the_default_still_migrates():
    with patch("agentpit.db.session.TableCreate.create_all_tables") as ddl:
        db = DbSession(TEST_DSN, min_size=0, max_idle=5.0)
        try:
            assert ddl.call_count == 1
        finally:
            db.close()
