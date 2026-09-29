import json
import uuid

from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.market_state import MarketState
from agentpit.db.table_write import TableWrite
from agentpit.services import account_service
from agentpit.services.account_service import AccountService
from tests.db_helpers import fresh_test_conn, fresh_test_db

CONDITION = "0x" + "ef" * 32


def _seed() -> str:
    """Three buys of one token at t 10, 20 and 30, a split at 25 and a merge at 35."""
    conn = fresh_test_conn()
    market = TableWrite.create_market(
        conn,
        CreateMarketRequest(
            question="Q?",
            description="d",
            erc1155_tokens=[("q-y", "Yes"), ("q-n", "No")],
            slug="q",
            condition_id=ConditionId(CONDITION),
            state=MarketState.ACTIVE,
        ),
        is_polygon_market=False,
    )
    _user_id, acct, key = TableWrite.create_user(conn, email="active@example.com", password_hash="x", handle=None)
    for t in (10, 20, 30):
        conn.execute(
            "INSERT INTO trades (TRADE_ID, MARKET, ASSET_ID, SIDE, PRICE, TRADE_SIZE, MATCH_KIND, STATUS, "
            "TAKER_API_KEY, MATCH_TIME) VALUES (%s, %s, 'q-y', 'BUY', 500000, 2000000, 'NORMAL', 'CONFIRMED', %s, %s)",
            (uuid.uuid4().hex, CONDITION, key, t),
        )
    for t, kind in ((25, "SPLIT"), (35, "MERGE")):
        conn.execute(
            "INSERT INTO transactions (TIMESTAMP, API_KEY, TRANSACTION_TYPE, MARKET_ID, DETAILS) "
            "VALUES (to_timestamp(%s), %s, %s, %s, %s)",
            (t, key, kind, market.market_id, json.dumps({"amount": 1_000_000})),
        )
    conn.close()
    return acct.address


def test_a_limit_returns_the_newest_rows_across_trades_and_transactions():
    address = _seed()
    accounts = AccountService(fresh_test_db(), onchain=None)
    assert [(a.timestamp, a.type) for a in accounts.list_activity(address, limit=2)] == [(35, "MERGE"), (30, "TRADE")]
    assert [(a.timestamp, a.type) for a in accounts.list_activity(address, limit=2, offset=1)] == [
        (30, "TRADE"),
        (25, "SPLIT"),
    ]
    assert [a.timestamp for a in accounts.list_activity(address, type_filter=["SPLIT"], limit=1)] == [25]


def test_each_token_resolves_once_per_read(monkeypatch):
    address = _seed()
    resolved: list[str] = []

    def counting(conn, token_id: str):
        resolved.append(token_id)
        return real(conn, token_id)

    real = account_service.resolve_by_token_id
    monkeypatch.setattr(account_service, "resolve_by_token_id", counting)
    acts = AccountService(fresh_test_db(), onchain=None).list_activity(address)
    assert [a.outcome for a in acts if a.type == "TRADE"] == ["Yes", "Yes", "Yes"]
    assert resolved == ["q-y"]
