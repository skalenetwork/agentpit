import uuid

from agentpit.config import Settings
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from tests.db_helpers import fresh_test_conn


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


def _rows_read_from_trades(node: dict) -> int:
    """Rows the scans of `trades` read, kept or filtered out: a walk of the
    time index that discards other tokens' prints returns few rows but reads
    the whole tape."""
    read = (node["Actual Rows"] + node.get("Rows Removed by Filter", 0)) * node["Actual Loops"]
    own = read if node.get("Relation Name") == "trades" else 0
    return own + sum(_rows_read_from_trades(child) for child in node.get("Plans", []))


def test_last_prices_read_one_print_per_token_and_side_however_long_the_tape():
    conn = fresh_test_conn()
    # 3,000 prints per market: YES on ASSET_ID, NO on MAKER_ASSET_ID; the
    # newest is at MATCH_TIME 3000 for market a and 2000 for market b.
    for yes, no, last in (("ya", "na", 3000), ("yb", "nb", 2000)):
        conn.execute(
            "INSERT INTO trades (TRADE_ID, ASSET_ID, MAKER_ASSET_ID, PRICE, TRADE_SIZE, SIDE, "
            "STATUS, MATCH_TIME, TAKER_API_KEY, MAKER_API_KEY) "
            "SELECT %s || g, %s, %s, 100000 + g, 1, 'BUY', 'MIRRORED', g, 'tk', 'mk' "
            "FROM generate_series(%s - 2999, %s) g",
            (yes, yes, no, last, last),
        )
    conn.execute("ANALYZE trades")
    tokens = ["ya", "na", "yb", "nb", "none"]
    sent = {}

    class _Spy:
        def execute(self, sql, params):
            sent.update(sql=sql, params=params)
            return conn.execute(sql, params)

    lasts = TableRead.last_trade_prices_for_tokens(_Spy(), tokens)
    plan = conn.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + sent["sql"], sent["params"]).fetchone()
    conn.close()

    assert lasts == {"ya": 103_000, "na": 897_000, "yb": 102_000, "nb": 898_000}
    assert _rows_read_from_trades(next(iter(plan.values()))[0]["Plan"]) <= 2 * len(tokens)
