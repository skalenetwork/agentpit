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
