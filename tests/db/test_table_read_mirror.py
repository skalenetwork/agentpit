import uuid

from agentpit.config import Settings
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead


def _insert_trade(conn, *, asset, price, match_time, status="MIRRORED"):
    conn.execute(
        "INSERT INTO trades (TRADE_ID, ASSET_ID, PRICE, MATCH_TIME, STATUS) "
        "VALUES (%s,%s,%s,%s,%s)",
        (uuid.uuid4().hex, asset, price, match_time, status),
    )


def test_last_trade_prices_for_tokens_latest_tape_print():
    db = DbSession(Settings().database_url)
    with db.write() as conn:
        _insert_trade(conn, asset="A", price=300_000, match_time=100)
        _insert_trade(conn, asset="A", price=320_000, match_time=200)  # latest print
        _insert_trade(
            conn, asset="A", price=999_000, match_time=300, status="CONFIRMED"
        )  # agent fill
        _insert_trade(conn, asset="B", price=500_000, match_time=50)
    with db.read() as conn:
        lasts = TableRead.last_trade_prices_for_tokens(conn, ["A", "B", "C"])
        assert TableRead.last_trade_prices_for_tokens(conn, []) == {}
    assert lasts == {"A": 320_000, "B": 500_000}
