"""create_all_tables builds the Postgres schema; BIGINT/SALT survive large values."""
import psycopg
import pytest
from eth_utils.crypto import keccak
from psycopg.rows import tuple_row

from agentpit.db.row_factory import ci_dict_row
from agentpit.db.table_create import TableCreate
from agentpit.db.table_read import TableRead
from agentpit.onchain.ctf_ids import condition_id
from tests.db_helpers import TEST_DSN

_ORACLE = "0x00000000000000000000000000000000000000a1"
_LEGACY_INSERT = (
    "INSERT INTO markets (CONDITION_ID, QUESTION, SLUG, DESCRIPTION, ERC1155_TOKENS, "
    "START_DATE, RESOLVED_OUTCOME) VALUES (%s, %s, %s, 'd', '[]', 0, %s)"
)
_COLUMNS = (
    "SELECT column_name, is_nullable FROM information_schema.columns WHERE table_name = "
    "'markets' AND column_name IN ('question_id', 'resolved_outcome') ORDER BY 1"
)


@pytest.fixture()
def conn():
    c = psycopg.connect(TEST_DSN, autocommit=True)
    c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    yield c
    c.close()


def test_creates_all_tables(conn):
    TableCreate.create_all_tables(conn)
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
    ).fetchall()
    names = {r[0] for r in rows}
    for t in ("users", "markets", "orders", "trades", "events", "transactions", "competition_points"):
        assert t in names


def test_bigint_amounts_round_trip(conn):
    TableCreate.create_all_tables(conn)
    big = 9_000_000_000_000          # > 2^31, would overflow INTEGER
    salt = str(2**255)               # 256-bit
    conn.execute(
        "INSERT INTO orders (ORDER_ID, MAKER_AMOUNT, TAKER_AMOUNT, REMAINING_AMOUNT, "
        "PRICE, SALT, CREATED_AT, STATUS) VALUES (%s,%s,%s,%s,%s,%s,%s,'live')",
        ("o1", big, big, big, 600000, salt, 1700000000),
    )
    row = conn.execute(
        "SELECT MAKER_AMOUNT, SALT FROM orders WHERE ORDER_ID='o1'"
    ).fetchone()
    assert row[0] == big and row[1] == salt


def test_idempotent(conn):
    TableCreate.create_all_tables(conn)
    TableCreate.create_all_tables(conn)  # second run must not error


def test_legacy_pending_trades_become_confirmed(conn):
    TableCreate.create_all_tables(conn)
    conn.execute(
        "INSERT INTO trades (TRADE_ID, STATUS, TRANSACTION_HASH) VALUES "
        "('legacy', 'PENDING', ''), ('unsent', 'PENDING', NULL), ('unknown', 'PENDING', '0xab')"
    )
    TableCreate.create_all_tables(conn)
    rows = dict(conn.execute("SELECT TRADE_ID, STATUS FROM trades").fetchall())
    assert rows == {"legacy": "CONFIRMED", "unsent": "PENDING", "unknown": "PENDING"}


def test_entries_captured_before_their_times_existed_are_dated_and_undatable_ones_go(conn):
    TableCreate.create_all_tables(conn)
    conn.execute("ALTER TABLE competition_entries DROP CONSTRAINT competition_entries_captured")
    conn.execute("INSERT INTO competitions (SLUG, TITLE, STARTS_AT, ENDS_AT, PRIZES) VALUES ('c', 'C', 100, 200, '[]')")
    conn.execute(
        "INSERT INTO competition_entries (SLUG, OWNER, USER_ID, ENROLLED_AT, START_RAW, END_RAW) VALUES "
        "('c', 'o1', 'a', 50, 1, 2), ('c', 'o2', 'b', 150, 1, NULL), ('c', 'o3', 'c', 50, NULL, NULL), "
        "('gone', 'o4', 'd', 50, 1, NULL), ('gone', 'o5', 'e', 50, NULL, NULL)"
    )

    TableCreate.create_all_tables(conn)

    assert conn.execute(
        "SELECT USER_ID, START_AT, END_AT, END_BLOCK FROM competition_entries ORDER BY USER_ID"
    ).fetchall() == [("a", 100, 200, None), ("b", 150, None, None), ("c", None, None, None), ("e", None, None, None)]


@pytest.mark.parametrize(
    "captured",
    [
        (1, None, None, None, None, None, None),
        (None, None, 1, 2, None, None, None),
        (1, 2, 1, None, None, None, None),
        (1, 2, None, None, 3, None, None),
        (None, None, None, None, None, "{}", None),
        (1, 2, None, None, None, None, "{}"),
    ],
    ids=[
        "start without its time", "end without a start", "end without its time", "block without an end",
        "start holdings without a start", "end holdings without an end",
    ],
)
def test_an_entry_holds_a_capture_only_with_its_time_and_an_end_only_after_a_start(conn, captured):
    TableCreate.create_all_tables(conn)

    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO competition_entries (SLUG, OWNER, USER_ID, ENROLLED_AT, START_RAW, START_AT, END_RAW, "
            "END_AT, END_BLOCK, START_HOLDINGS, END_HOLDINGS) VALUES ('c', 'o', 'u', 0, %s, %s, %s, %s, %s, %s, %s)",
            captured,
        )


def _legacy_markets(conn, *rows: tuple[str, str, int | None]) -> None:
    TableCreate.create_all_tables(conn)
    conn.execute("ALTER TABLE markets ALTER COLUMN QUESTION_ID DROP NOT NULL")
    conn.execute("ALTER TABLE markets ADD COLUMN RESOLVED_OUTCOME INTEGER")
    for cid, question, outcome in rows:
        conn.execute(_LEGACY_INSERT, (cid, question, question, outcome))


def _cid(question: str) -> str:
    return "0x" + condition_id(_ORACLE, keccak(text=question), 2).hex()


def test_a_foreign_condition_id_aborts_the_market_migration_untouched(conn):
    _legacy_markets(conn, (_cid("Q?"), "Q?", 0), ("0x" + "ab" * 32, "Other?", None))
    with pytest.raises(RuntimeError, match="market 2 has CONDITION_ID 0xabab"):
        with conn.transaction():
            TableCreate.create_all_tables(conn)
            TableCreate.migrate_markets(conn, _ORACLE)

    assert conn.execute(_COLUMNS).fetchall() == [
        ("question_id", "YES"),
        ("resolved_outcome", "YES"),
    ]
    assert conn.execute(
        "SELECT QUESTION_ID, RESOLVED_OUTCOME, PAYOUTS FROM markets ORDER BY MARKET_ID"
    ).fetchall() == [(None, 0, None), (None, None, None)]


def test_the_market_migration_backfills_question_ids_and_payouts(conn):
    _legacy_markets(
        conn, (_cid("Yes?"), "Yes?", 0), (_cid("No?"), "No?", 1), (_cid("Open?"), "Open?", None)
    )
    TableCreate.migrate_markets(conn, _ORACLE)
    TableCreate.migrate_markets(conn, _ORACLE)

    assert conn.execute(_COLUMNS).fetchall() == [("question_id", "NO")]
    rows = conn.execute("SELECT QUESTION, QUESTION_ID, PAYOUTS FROM markets").fetchall()
    assert {q: (qid, payouts) for q, qid, payouts in rows} == {
        "Yes?": ("0x" + keccak(text="Yes?").hex(), [1, 0]),
        "No?": ("0x" + keccak(text="No?").hex(), [0, 1]),
        "Open?": ("0x" + keccak(text="Open?").hex(), None),
    }


class _Explained:
    """A connection that records the plan of each query it runs."""

    def __init__(self, conn: psycopg.Connection, target=None, plans: list[str] | None = None):
        self._conn, self._target, self.plans = conn, target or conn, [] if plans is None else plans

    def execute(self, query, params=None):
        with self._conn.cursor(row_factory=tuple_row) as cur:
            self.plans.append(" ".join(row[0] for row in cur.execute(f"EXPLAIN {query}", params)))
        return self._target.execute(query, params)

    def cursor(self, **kwargs):
        return _Explained(self._conn, self._conn.cursor(**kwargs), self.plans)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._target.close()


def test_the_race_reads_probe_the_fill_redeem_snapshot_and_point_indexes(conn):
    TableCreate.create_all_tables(conn)
    db = psycopg.connect(TEST_DSN, autocommit=True, row_factory=ci_dict_row)
    db.execute("SET enable_seqscan = off")
    explained = _Explained(db)

    TableRead.entries(explained, ["october-test"])
    TableRead.points(explained, "october-test", 0)
    TableRead.recent_activity(explained, [("key", 0, 2**62)], 20)
    db.close()

    entries, points, tape = explained.plans
    assert "idx_trades_fills_taker_time" in entries and "idx_account_snapshots_user_t" in entries
    assert "competition_points_pkey" in points
    assert "idx_trades_fills_taker_time" in tape and "idx_transactions_redeems" in tape
    assert not any("Seq Scan on trades" in plan or "Seq Scan on transactions" in plan for plan in explained.plans)
