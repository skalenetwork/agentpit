from agentpit.config import Settings
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.liquidity.tape import MIRROR_API_KEY, MIRROR_TRADE_STATUS, insert_mirrored_trades
from tests.onchain._helpers import create_market, fresh_client


def test_mirrored_trade_feeds_last_trade_price_but_no_user_feed():
    client = fresh_client()
    m = create_market(client)
    cond = m["condition_id"]["value"]
    yes_token, no_token = m["erc1155_tokens"][0][0], m["erc1155_tokens"][1][0]

    db = DbSession(Settings().database_url)
    with db.write() as conn:
        insert_mirrored_trades(conn, [
            (cond, yes_token, no_token, 470_000, 1_000_000, "SELL", 1_699_999_999),
            (cond, yes_token, no_token, 480_000, 2_500_000, "BUY", 1_700_000_000),
        ])

    with db.read() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE ASSET_ID = %s ORDER BY MATCH_TIME",
            (yes_token,)).fetchall()
    assert len(rows) == 2 and rows[0]["TRADE_ID"] != rows[1]["TRADE_ID"]
    row = rows[1]
    assert row["STATUS"] == MIRROR_TRADE_STATUS
    assert row["TAKER_API_KEY"] == MIRROR_API_KEY      # never a real user's key
    assert int(row["PRICE"]) == 480_000
    assert row["MAKER_ASSET_ID"] == no_token
    assert row["MATCH_KIND"] == "NORMAL"

    # Token-scoped readers see it, and NO prints at 1 - p...
    book = client.get(f"/book?token_id={yes_token}").json()
    assert book["last_trade_price"] == "0.48"
    book = client.get(f"/book?token_id={no_token}").json()
    assert book["last_trade_price"] == "0.52"

    # ...user-scoped feeds can't: trades are keyed by real API keys only.
    with db.read() as conn:
        rows = TableRead.list_trades_for_api_key(conn, "any-real-user-key")
    assert not rows
