import json

from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from tests.db_helpers import fresh_test_conn


def _insert_market(conn) -> int:
    row = conn.execute(
        """
        INSERT INTO markets
            (CONDITION_ID, QUESTION_ID, QUESTION, SLUG, DESCRIPTION, ERC1155_TOKENS,
             START_DATE, END_DATE, MARKET_STATE)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'ACTIVE')
        RETURNING MARKET_ID
        """,
        (
            "0x" + "ab" * 32, "0x" + "ab" * 32, "Q?", "q", "d",
            json.dumps([["1", "YES"], ["2", "NO"]]),
            1000, 2000,
        ),
    ).fetchone()
    return row["MARKET_ID"]


def test_fully_redeemed_defaults_false_then_marks_true():
    conn = fresh_test_conn()
    mid = _insert_market(conn)

    market = TableRead.read_market(conn, mid)
    assert market is not None
    assert market.fully_redeemed is False

    TableWrite.mark_fully_redeemed(conn, mid)

    market2 = TableRead.read_market(conn, mid)
    assert market2 is not None
    assert market2.fully_redeemed is True
