import json

from agentpit.db.table_read import TableRead
from tests.db_helpers import fresh_test_conn


def _insert_market(conn, *, cid, state, end_date, tokens, fully=False) -> int:
    # RESOLVED markets require PAYOUTS per the Market domain invariant.
    payouts = [0, 1] if state == "RESOLVED" else None
    row = conn.execute(
        """
        INSERT INTO markets
            (CONDITION_ID, QUESTION_ID, QUESTION, SLUG, DESCRIPTION, ERC1155_TOKENS,
             START_DATE, END_DATE, MARKET_STATE, FULLY_REDEEMED, PAYOUTS)
        VALUES (%s, %s, 'Q?', %s, 'd', %s, 100, %s, %s, %s, %s)
        RETURNING MARKET_ID
        """,
        (cid, cid, cid, json.dumps(tokens), end_date, state, fully, payouts),
    ).fetchone()
    return row["MARKET_ID"]


def test_list_resolved_unredeemed_markets():
    conn = fresh_test_conn()
    open_resolved = _insert_market(conn, cid="0x11", state="RESOLVED",
                                   end_date=500, tokens=[["1", "YES"], ["2", "NO"]])
    _insert_market(conn, cid="0x12", state="RESOLVED", end_date=500,
                   tokens=[["3", "YES"], ["4", "NO"]], fully=True)  # done
    _insert_market(conn, cid="0x13", state="ACTIVE", end_date=500,
                   tokens=[["5", "YES"], ["6", "NO"]])  # not resolved

    out = TableRead.list_resolved_unredeemed_markets(conn)
    assert [m.market_id for m in out] == [open_resolved]


def test_list_participant_api_keys_for_market():
    conn = fresh_test_conn()
    mid = _insert_market(conn, cid="0x21", state="RESOLVED", end_date=500,
                         tokens=[["100", "YES"], ["200", "NO"]])
    conn.execute(
        "INSERT INTO trades (TRADE_ID, ASSET_ID, TAKER_API_KEY, MAKER_API_KEY, "
        "STATUS, MATCH_TIME) VALUES ('t1', '100', 'alice', 'bob', 'MATCHED', 1)"
    )
    conn.execute(
        "INSERT INTO trades (TRADE_ID, ASSET_ID, TAKER_API_KEY, MAKER_API_KEY, "
        "STATUS, MATCH_TIME) VALUES ('t2', '999', 'carol', 'dave', 'MATCHED', 2)"
    )  # different token -> excluded
    conn.execute(
        "INSERT INTO transactions (API_KEY, TRANSACTION_TYPE, MARKET_ID) "
        "VALUES ('eve', 'SPLIT', %s)", (mid,)
    )

    keys = TableRead.list_participant_api_keys_for_market(
        conn, mid, ["100", "200"]
    )
    assert keys == {"alice", "bob", "eve"}
