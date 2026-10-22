"""Shared test-database helpers for the Postgres-backed suite.

Replaces the old `DbSession(':memory:')` / `sqlite3.connect(':memory:')`
fixtures. Tests run against a real local Postgres (`agentpit_test`); the
autouse fixture in conftest truncates every table before each test, so a
plain pool/connection on the shared test DB is already isolated.
"""

import os
import time

import psycopg

from agentpit.datastructures.competition import Competition
from agentpit.db.row_factory import ci_dict_row
from agentpit.db.session import DbSession
from agentpit.db.table_create import TableCreate
from agentpit.db.table_write import TableWrite

TEST_DSN = os.environ.get("AGENTPIT_DATABASE_URL", "postgresql:///agentpit_test")


def fresh_test_db() -> DbSession:
    """A DbSession (psycopg pool) on the test database (schema ensured by
    __init__). Replaces `DbSession(':memory:')`. min_size=0 + short max_idle
    so leaked/idle test pools shed their Postgres connections quickly."""
    return DbSession(TEST_DSN, min_size=0, max_idle=5.0)


def fresh_test_conn() -> psycopg.Connection:
    """A raw psycopg connection on the test DB (autocommit + case-insensitive
    dict rows) with the schema ensured. Replaces
    `sqlite3.connect(':memory:')` DAL fixtures."""
    conn = psycopg.connect(TEST_DSN, autocommit=True, row_factory=ci_dict_row)
    TableCreate.create_all_tables(conn)
    return conn


def enter_ended(db: DbSession, user_id: str, *, started: bool = True) -> None:
    """Enter the account in "Bell test", which ended a minute ago, with its
    start value taken unless `started` is off: a started entry halts the
    account's trading until `take_end`."""
    now = int(time.time())
    with db.write() as conn:
        TableWrite.save_competition(
            conn, Competition(slug="bell", title="Bell test", starts_at=now - 3_600, ends_at=now - 60, prizes=[]), 0
        )
        TableWrite.enter_competition(conn, "bell", "user_owner", user_id, now - 7_200, False)
        if started:
            TableWrite.capture_entry(conn, "bell", user_id, "START", 1, now - 3_600, None)


def take_end(db: DbSession, user_id: str) -> None:
    """Take the account's end value in "Bell test", which lifts its halt."""
    with db.write() as conn:
        TableWrite.capture_entry(conn, "bell", user_id, "END", 1, int(time.time()), 1)
