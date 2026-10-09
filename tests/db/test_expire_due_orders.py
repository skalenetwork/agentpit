"""Dead rows stop being live, so they can eventually be deleted.

This sweep carries no correctness: the read predicate already excludes every
row it touches. Its whole job is that the table does not fill with orders
that will never trade again. So it may run late, run rarely, or fail; none
of that trades anything.
"""
import time

from agentpit.config import Settings
from agentpit.db.session import DbSession
from agentpit.db.table_write import TableWrite


def _order(conn, *, order_id: str, expiration: int | None) -> None:
    conn.execute(
        "INSERT INTO orders (ORDER_ID, TOKEN_ID, SIDE, PRICE, STATUS, "
        "REMAINING_AMOUNT, EXPIRATION, CREATED_AT, API_KEY) "
        "VALUES (%s, 'tok', 'BUY', 500000, 'live', 1000000, %s, %s, 'k')",
        (order_id, expiration, int(time.time())),
    )


def _status(conn, order_id: str) -> str:
    return conn.execute(
        "SELECT STATUS FROM orders WHERE ORDER_ID = %s", (order_id,)
    ).fetchone()["STATUS"]


def test_it_marks_exactly_what_the_read_predicate_already_hides():
    db = DbSession(Settings().database_url)
    now = int(time.time())
    with db.write() as conn:
        _order(conn, order_id="o-dead", expiration=now + 30)   # inside the grace
        _order(conn, order_id="o-alive", expiration=now + 600)  # still trading
        _order(conn, order_id="o-never", expiration=0)          # GTC
        marked = TableWrite.expire_due_orders(conn, now)

        assert marked == 1
        assert _status(conn, "o-dead") == "cancelled"
        assert _status(conn, "o-alive") == "live"
        assert _status(conn, "o-never") == "live"


def test_it_leaves_a_null_expiration_alone():
    """A NULL EXPIRATION means "never expires", same as 0 — `LIVE_ORDER`
    treats them identically. `EXPIRATION > 0` is false for NULL rows in SQL
    (`NULL > 0` is NULL, which WHERE treats as false), so the sweep must
    never touch them, exactly like it never touches the 0 (GTC) row above."""
    db = DbSession(Settings().database_url)
    now = int(time.time())
    with db.write() as conn:
        _order(conn, order_id="o-null", expiration=None)
        marked = TableWrite.expire_due_orders(conn, now)

        assert marked == 0
        assert _status(conn, "o-null") == "live"
