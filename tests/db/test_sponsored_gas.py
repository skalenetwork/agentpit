from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from tests.db_helpers import fresh_test_conn


def test_gas_adds_up_per_account_and_day():
    conn = fresh_test_conn()
    TableWrite.add_sponsored_gas(conn, "k1", 100, 5)
    TableWrite.add_sponsored_gas(conn, "k1", 100, 7)
    TableWrite.add_sponsored_gas(conn, "k1", 101, 3)
    TableWrite.add_sponsored_gas(conn, "k2", 100, 1)
    assert TableRead.sponsored_gas_used(conn, "k1", 100) == 12
    assert TableRead.sponsored_gas_used(conn, "k1", 101) == 3
    assert TableRead.sponsored_gas_used(conn, "k3", 100) == 0
    conn.close()


def test_reservation_is_refused_once_the_budget_is_used():
    conn = fresh_test_conn()
    assert TableWrite.reserve_sponsored_gas(conn, "k", 200, 600, 1_000)      # 0 < 1000 -> 600
    assert TableWrite.reserve_sponsored_gas(conn, "k", 200, 600, 1_000)      # 600 < 1000 -> 1200
    assert not TableWrite.reserve_sponsored_gas(conn, "k", 200, 600, 1_000)  # 1200 >= 1000
    assert TableRead.sponsored_gas_used(conn, "k", 200) == 1_200
    assert TableWrite.reserve_sponsored_gas(conn, "k", 201, 600, 1_000)      # a new day starts at 0
    conn.close()


def test_non_bot_api_keys_drops_the_house_and_unknown_keys():
    conn = fresh_test_conn()
    _u, _a, human = TableWrite.create_user(conn, email="h@example.com", password_hash=None, handle=None)
    _u2, _a2, house = TableWrite.create_user(conn, email="house@example.com", password_hash=None, handle=None)
    TableWrite.mark_user_as_bot(conn, house)
    assert TableRead.non_bot_api_keys(conn, [human, house, "nope"]) == {human}
    conn.close()
