"""create_all_tables builds the Postgres schema; BIGINT/SALT survive large values."""
import psycopg
import pytest
from agentpit.db.table_create import TableCreate
from tests.db_helpers import TEST_DSN


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
