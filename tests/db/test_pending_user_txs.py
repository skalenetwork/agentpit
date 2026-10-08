"""`pending_user_txs`: a user transaction that was signed and may be on its way
to the chain, before anybody knows how it ended.

`PositionService` writes the row just before the broadcast and confirms it once
the receipt is in; the auto-redeem pass confirms or drops what is left. The
confirmation is one statement, so of two confirmers racing on one hash exactly
one writes the transactions row.
"""

import json

from agentpit.db.table_create import TableCreate
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from tests.db_helpers import fresh_test_conn

_HASH = "0x" + "aa" * 32
_OTHER = "0x" + "bb" * 32


def _transactions(conn) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT API_KEY, TRANSACTION_TYPE, MARKET_ID, DETAILS FROM transactions "
            "ORDER BY TRANSACTION_ID"
        ).fetchall()
    ]


def test_the_table_is_created_idempotently():
    conn = fresh_test_conn()
    TableCreate.create_all_tables(conn)
    TableCreate.create_all_tables(conn)
    cols = {
        r["column_name"]: (r["data_type"], r["is_nullable"])
        for r in conn.execute(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'pending_user_txs'"
        ).fetchall()
    }
    assert cols == {
        "tx_hash": ("text", "NO"),
        "api_key": ("text", "NO"),
        "transaction_type": ("text", "NO"),
        "market_id": ("bigint", "YES"),
        "details": ("text", "YES"),
        "created_at": ("bigint", "NO"),
    }
    conn.close()


def test_a_row_reads_back_as_it_was_written():
    conn = fresh_test_conn()
    TableWrite.insert_pending_user_tx(
        conn, _HASH, "k1", "SPLIT", 7, {"amount": 40_000_000}, created_at=1_000
    )
    TableWrite.insert_pending_user_tx(conn, _OTHER, "k2", "REDEEM", 8, {}, created_at=999)

    rows = TableRead.list_pending_user_txs(conn)

    # Oldest first.
    assert [(r.tx_hash, r.api_key, r.transaction_type, r.market_id) for r in rows] == [
        (_OTHER, "k2", "REDEEM", 8),
        (_HASH, "k1", "SPLIT", 7),
    ]
    assert rows[0].details == {}  # a claim's details carry no amount yet
    assert rows[1].details == {"amount": 40_000_000}
    assert (rows[0].created_at, rows[1].created_at) == (999, 1_000)
    conn.close()


def test_has_pending_is_per_account_and_market_and_only_since_the_cutoff():
    conn = fresh_test_conn()
    TableWrite.insert_pending_user_tx(conn, _HASH, "k1", "REDEEM", 7, {}, created_at=1_000)

    assert TableRead.has_pending_user_tx(conn, "k1", 7, since=1_000) is True
    assert TableRead.has_pending_user_tx(conn, "k1", 7, since=1_001) is False  # too old
    assert TableRead.has_pending_user_tx(conn, "k1", 8, since=0) is False  # other market
    assert TableRead.has_pending_user_tx(conn, "k2", 7, since=0) is False  # other account
    conn.close()


def test_delete_says_whether_there_was_a_row():
    conn = fresh_test_conn()
    TableWrite.insert_pending_user_tx(conn, _HASH, "k1", "MERGE", 7, {"amount": 1}, created_at=1)

    assert TableWrite.delete_pending_user_tx(conn, _HASH) is True
    assert TableWrite.delete_pending_user_tx(conn, _HASH) is False
    assert TableRead.list_pending_user_txs(conn) == []
    assert _transactions(conn) == []
    conn.close()


def test_confirming_moves_the_row_into_transactions_exactly_once():
    conn = fresh_test_conn()
    TableWrite.insert_pending_user_tx(conn, _HASH, "k1", "REDEEM", 7, {}, created_at=1)
    TableWrite.insert_pending_user_tx(conn, _OTHER, "k1", "SPLIT", 8, {"amount": 5}, created_at=1)

    assert TableWrite.confirm_pending_user_tx(
        conn, _HASH, {"collateral_amount": 100_000_000}
    ) is True
    # The second confirmer of the same hash (the service and the reconciler
    # racing) finds the row gone and writes nothing.
    assert TableWrite.confirm_pending_user_tx(
        conn, _HASH, {"collateral_amount": 100_000_000}
    ) is False

    assert _transactions(conn) == [
        {
            "api_key": "k1",
            "transaction_type": "REDEEM",
            "market_id": 7,
            "details": json.dumps({"collateral_amount": 100_000_000}),
        }
    ]
    assert [r.tx_hash for r in TableRead.list_pending_user_txs(conn)] == [_OTHER]
    conn.close()
