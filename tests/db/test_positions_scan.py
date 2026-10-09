"""TableRead.list_markets_with_user_activity scopes the position scan to only
the markets a user has touched — the fix for list_positions doing an O(market
count) on-chain balance scan (every market) that ran ~24s at 1000 markets."""
import uuid

from agentpit.config import Settings
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite


def _insert_market(conn, *, condition_id: str, slug: str) -> int:
    return conn.execute(
        "INSERT INTO markets (CONDITION_ID, QUESTION_ID, QUESTION, SLUG, DESCRIPTION, "
        "ERC1155_TOKENS, START_DATE) VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING MARKET_ID",
        (condition_id, condition_id, "Q?", slug, "", '[["111","Yes"],["222","No"]]', 0),
    ).fetchone()["MARKET_ID"]


def _insert_trade(conn, *, market: str, taker: str, maker: str) -> None:
    conn.execute(
        "INSERT INTO trades (TRADE_ID, MARKET, ASSET_ID, TAKER_API_KEY, "
        "MAKER_API_KEY, STATUS, PRICE, MATCH_TIME) "
        "VALUES (%s,%s,'tok',%s,%s,'MATCHED',100000,100)",
        (uuid.uuid4().hex, market, taker, maker),
    )


def test_scopes_to_markets_the_user_traded_or_split():
    db = DbSession(Settings().database_url)
    with db.write() as conn:
        traded = _insert_market(conn, condition_id="0xAAA", slug="traded")
        split = _insert_market(conn, condition_id="0xBBB", slug="split")
        _insert_market(conn, condition_id="0xCCC", slug="untouched")
        # k1 took a fill in `traded` and split in `split`.
        _insert_trade(conn, market="0xAAA", taker="k1", maker="house")
        TableWrite.log_transaction(conn, "k1", "SPLIT", split, {"amount": 1})
        # An unrelated user's trade in `untouched` must NOT leak into k1's scan.
        _insert_trade(conn, market="0xCCC", taker="k2", maker="house")
    with db.read() as conn:
        ids = {
            m.market_id
            for m in TableRead.list_markets_with_user_activity(conn, "k1")
        }
    assert ids == {traded, split}


def test_maker_side_counts_too():
    db = DbSession(Settings().database_url)
    with db.write() as conn:
        m = _insert_market(conn, condition_id="0xDDD", slug="as-maker")
        _insert_trade(conn, market="0xDDD", taker="other", maker="k1")
    with db.read() as conn:
        ids = {p.market_id for p in TableRead.list_markets_with_user_activity(conn, "k1")}
    assert ids == {m}


def test_empty_when_user_has_no_activity():
    db = DbSession(Settings().database_url)
    with db.write() as conn:
        _insert_market(conn, condition_id="0xEEE", slug="lonely")
    with db.read() as conn:
        assert TableRead.list_markets_with_user_activity(conn, "nobody") == []
