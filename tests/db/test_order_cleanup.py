from agentpit.config import Settings
from agentpit.db.session import DbSession
from agentpit.db.table_write import TableWrite


def test_purge_mirrored_trades_removes_only_old_mirrored_in_bounded_chunks():
    db = DbSession(Settings().database_url)
    with db.write() as conn:
        conn.execute(
            "INSERT INTO trades (TRADE_ID, STATUS, MATCH_TIME) VALUES "
            "('old1', 'MIRRORED', 100), ('old2', 'MIRRORED', 200), "
            "('old3', 'MIRRORED', 300), ('new', 'MIRRORED', 900), "
            "('user', 'CONFIRMED', 100)"
        )
        assert TableWrite.purge_mirrored_trades(conn, 500, limit=2) == 2
        assert TableWrite.purge_mirrored_trades(conn, 500, limit=2) == 1
    with db.read() as conn:
        kept = {r["TRADE_ID"] for r in conn.execute("SELECT TRADE_ID FROM trades")}
    assert kept == {"new", "user"}


def _orders(db: DbSession) -> dict[str, tuple[str, int]]:
    with db.read() as conn:
        rows = conn.execute(
            "SELECT ORDER_ID, STATUS, REMAINING_AMOUNT FROM orders"
        ).fetchall()
    return {r["ORDER_ID"]: (r["STATUS"], r["REMAINING_AMOUNT"]) for r in rows}


def test_the_cutover_deletes_only_the_houses_unmatched_rows_and_is_safe_twice():
    db = DbSession(Settings().database_url)
    with db.write() as conn:
        conn.execute(
            "INSERT INTO orders (ORDER_ID, API_KEY, STATUS, REMAINING_AMOUNT, EXPIRATION, CREATED_AT) VALUES "
            "('h-live', 'house', 'live', 1, 0, 0), ('h-gone', 'house', 'cancelled', 1, 0, 0), "
            "('h-filled', 'house', 'matched', 0, 0, 0), ('a-live', 'agent', 'live', 1, 0, 0)"
        )
        assert TableWrite.delete_house_orders(conn, "house") == 2
        assert TableWrite.delete_house_orders(conn, "house") == 0
    assert set(_orders(db)) == {"h-filled", "a-live"}


def test_a_failed_fill_cancels_the_remainder_of_its_order():
    db = DbSession(Settings().database_url)
    with db.write() as conn:
        conn.execute(
            "INSERT INTO orders (ORDER_ID, API_KEY, STATUS, REMAINING_AMOUNT, EXPIRATION, CREATED_AT) VALUES "
            "('lost', 'a', 'live', 6, 0, 0), ('kept', 'a', 'live', 6, 0, 0)"
        )
        conn.execute(
            "INSERT INTO trades (TRADE_ID, TAKER_ORDER_ID, STATUS, MATCH_TIME) VALUES "
            "('t-lost', 'lost', 'PENDING', 0), ('t-kept', 'kept', 'PENDING', 0)"
        )
        TableWrite.settle_trades(conn, ["t-lost"], "FAILED", None)
        TableWrite.settle_trades(conn, ["t-kept"], "CONFIRMED", "0xab")
    assert _orders(db) == {"lost": ("cancelled", 6), "kept": ("live", 6)}
