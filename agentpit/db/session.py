from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg_pool import ConnectionPool

from agentpit.db.row_factory import ci_dict_row
from agentpit.db.table_create import TableCreate


class DbSession:
    """Owns a psycopg connection pool. `read()`/`write()` each check out a
    pooled connection; psycopg's connection context commits on clean exit and
    rolls back on exception. No global mutex — the pool gives each thread its
    own connection, so reads and writes run concurrently.
    """

    # Registry of currently-open sessions. The test suite uses it to close
    # pools leaked by per-test `create_app()` calls (which never run their
    # lifespan shutdown), so they don't pin Postgres connections.
    _open: "list[DbSession]" = []

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 2,
        max_size: int = 16,
        max_idle: float = 600.0,
        create_tables: bool = True,
        statement_timeout_ms: int | None = None,
        idle_in_transaction_timeout_ms: int | None = None,
    ):
        """`statement_timeout_ms` cancels any statement on a pooled connection
        that runs longer than this many milliseconds; None means no limit.
        `idle_in_transaction_timeout_ms` ends the session of a connection whose
        transaction sits open and idle that long; None means no limit.

        `create_tables=False` opens the pool without running the DDL.

        The DDL is idempotent but not free: every `ALTER TABLE ... ADD COLUMN
        IF NOT EXISTS` takes an AccessExclusiveLock even when the column is
        already there. That is harmless for the API, which runs it once before
        serving, but a one-off script that opens its own session against a
        LIVE database is a second writer racing the API for locks on every
        table — and it deadlocks:

            DeadlockDetected: process A waits for AccessExclusiveLock on
            markets; blocked by process B. Process B waits for
            AccessShareLock on a relation A already holds.

        Scripts in `scripts/` connect to a database the API has already
        migrated, so they should pass False and touch nothing but their rows.
        """
        # `-c jit=off`: Postgres decides whether to JIT-compile from the
        # planner's ESTIMATED cost, and on this schema that estimate is
        # inflated three orders of magnitude by the mirror tape's share of
        # `trades` -- the same bad statistic documented on
        # `TableRead.TRADED_ACCOUNTS_SQL`. Measured on production, the
        # leaderboard's trade counts spent 220ms in LLVM to do 2ms of work.
        # Set per-connection rather than on the server so the choice lives
        # in the repository beside the queries it protects;
        # `tests/db/test_session_jit.py` carries the measurement.
        # `-c plan_cache_mode=force_custom_plan`: psycopg prepares a
        # statement after five runs, and Postgres then reuses a generic
        # plan that cannot see the parameters. For the leaderboard's
        # traded-accounts query that plan scans `trades` once per user who
        # never traded: 3.3 s on production on 2026-10-14, against 1 ms.
        options = "-c jit=off -c plan_cache_mode=force_custom_plan"
        # Opt-in per-connection limits. A runaway statement is cancelled and
        # a transaction left open and idle is ended, so neither can hold one
        # of the pool's connections indefinitely. None leaves the server's
        # default (no limit) for scripts and tests.
        if statement_timeout_ms is not None:
            options += f" -c statement_timeout={int(statement_timeout_ms)}"
        if idle_in_transaction_timeout_ms is not None:
            options += (
                f" -c idle_in_transaction_session_timeout={int(idle_in_transaction_timeout_ms)}"
            )
        self._pool = ConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            max_idle=max_idle,
            kwargs={
                "row_factory": ci_dict_row,
                "autocommit": False,
                "options": options,
            },
            open=True,
        )
        if create_tables:
            with self._pool.connection() as conn:
                # Schema changes may index or rewrite a large table; they get
                # no statement timeout. SET LOCAL ends with this transaction.
                conn.execute("SET LOCAL statement_timeout = 0")
                TableCreate.create_all_tables(conn)
        DbSession._open.append(self)

    @contextmanager
    def read(self) -> Iterator[psycopg.Connection]:
        with self._pool.connection() as conn:
            yield conn

    @contextmanager
    def write(self) -> Iterator[psycopg.Connection]:
        with self._pool.connection() as conn:
            yield conn

    def close(self) -> None:
        self._pool.close()
        try:
            DbSession._open.remove(self)
        except ValueError:
            pass
