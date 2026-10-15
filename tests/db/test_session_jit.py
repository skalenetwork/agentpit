"""Every pooled connection runs with JIT off.

Postgres decides whether to JIT-compile a query from the planner's *estimated*
cost, before it knows what the query will actually do. On this schema that
estimate is wrong by three orders of magnitude, and the mistake is expensive in
exactly the wrong direction.

Measured on production 2026-08-12, `TableRead.count_trades_by_user` -- three
index probes per account, every buffer already in cache, two milliseconds of
actual work:

    GroupAggregate  (cost=68998.04..1324283.22 rows=18)
      ->  Index Scan using users_pkey on users u  (actual time=232.084..232.096)
            Buffers: shared hit=2
    JIT:
      Functions: 24
      Timing: Generation 1.069 ms, Inlining 46.069 ms,
              Optimization 104.678 ms, Emission 67.801 ms, Total 219.616 ms
    Execution Time: 243.874 ms

Two buffers read and 230ms spent before the first row: that is LLVM, not I/O.
The same query with `jit=off` runs in 11.8ms.

The estimate of 1,324,283 clears `jit_above_cost` (100,000) by a factor of
thirteen, and it is inflated for the same reason documented on
`TableRead.TRADED_ACCOUNTS_SQL`: 99.76% of `trades` is the liquidity mirror's
synthetic tape under a single api key, so the planner believes any given key
matches ~87,000 rows. One bad statistic, two different symptoms -- there it
chose a sequential scan, here it compiles a two-millisecond query.

JIT earns its keep on analytical queries that run for seconds. Nothing here
does: this is OLTP against a small working set, so the compiler is pure cost.
Setting it per-connection rather than on the server keeps the decision in the
repository, scoped to our own workload, and out of the database's catalog where
nobody reading this code would find it.
"""
from agentpit.config import Settings
from agentpit.db.session import DbSession


def test_a_pooled_connection_has_jit_off():
    db = DbSession(Settings().database_url)
    with db.read() as conn:
        assert conn.execute("SHOW jit").fetchone()["jit"] == "off"
        assert conn.execute("SHOW plan_cache_mode").fetchone()["plan_cache_mode"] == "force_custom_plan"


def test_a_write_connection_has_it_too():
    # `read()` and `write()` check out of the same pool today, so this looks
    # redundant -- and it is, until somebody gives writes their own pool and
    # configures it separately. The whole point is that no connection the
    # application opens is exempt.
    db = DbSession(Settings().database_url)
    with db.write() as conn:
        assert conn.execute("SHOW jit").fetchone()["jit"] == "off"
