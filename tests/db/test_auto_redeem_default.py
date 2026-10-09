"""Auto-redeem is on by default for every account (owner decision 2026-10-08).

Claims are sponsored now, so collecting winnings automatically costs the
account nothing. An account that never opens Settings still gets paid, and
the toggle stays for anyone who would rather claim by hand.
"""

from agentpit.db.table_create import TableCreate
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from tests.db_helpers import fresh_test_conn


def _column_default(conn) -> str | None:
    row = conn.execute(
        "SELECT column_default FROM information_schema.columns "
        "WHERE table_name = 'users' AND column_name = 'auto_redeem_enabled'"
    ).fetchone()
    return None if row is None else row["column_default"]


def test_a_database_made_under_the_old_default_is_moved_to_the_new_one():
    """ADD COLUMN IF NOT EXISTS never revisits a column that already exists, so
    the default needs a statement of its own. Without one, every database
    created before this change would keep opting new accounts out."""
    conn = fresh_test_conn()
    conn.execute("ALTER TABLE users ALTER COLUMN AUTO_REDEEM_ENABLED SET DEFAULT FALSE")

    TableCreate.create_all_tables(conn)

    assert _column_default(conn) == "true"
    conn.close()


def test_an_account_can_still_switch_it_off():
    conn = fresh_test_conn()
    user_id, _acct, _key = TableWrite.create_user(
        conn, email="optout@example.com", password_hash="x", handle=None
    )
    assert TableWrite.set_auto_redeem(conn, user_id, False)

    user = TableRead.get_user_by_userid(conn, user_id)
    assert user is not None and user.auto_redeem is False
    conn.close()
