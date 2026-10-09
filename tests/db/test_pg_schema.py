"""create_all_tables builds the Postgres schema; BIGINT/SALT survive large values."""
import psycopg
import pytest
from eth_utils.crypto import keccak

from agentpit.db.table_create import TableCreate
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
    for t in ("users", "markets", "orders", "trades", "events", "transactions"):
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
