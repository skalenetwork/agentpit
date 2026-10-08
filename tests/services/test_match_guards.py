import secrets

import pytest

from agentpit.config import Settings
from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.market_state import MarketState
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import OrderNotFilledError
from agentpit.onchain.order_signer import OrderData
from agentpit.services.order_service import OrderService
from tests.db_helpers import fresh_test_db

YES = "99" + "0" * 20
NO = "99" + "0" * 19 + "1"
COND = "0x" + "cd" * 32
ZERO = "0x" + "00" * 20

# `_insert_order` is an instance method that only touches the connection it is
# handed, so one db-less service is enough to write orders directly.
_WRITER = OrderService(None, None, Settings())  # type: ignore[arg-type]


def _db():
    db = fresh_test_db()
    with db.write() as conn:
        TableWrite.create_market(
            conn,
            CreateMarketRequest(question="Self?", description="d", erc1155_tokens=[(YES, "Yes"), (NO, "No")],
                                slug="self", condition_id=ConditionId(COND), state=MarketState.ACTIVE),
            is_polygon_market=False,
        )
    return db


def _account(db, *, workos=None, owner=None) -> str:
    with db.write() as conn:
        user_id, _a, api_key = TableWrite.create_user(
            conn, email=None if owner else f"{secrets.token_hex(4)}@x.com", password_hash=None,
            handle=None, owner_workos_id=owner,
        )
        if workos:
            TableWrite.set_workos_user_id(conn, user_id, workos)
    return api_key


def _order(conn, api_key, token, side, price_micro, size_micro, order_type="GTC") -> str:
    collateral = price_micro * size_micro // 1_000_000
    order = OrderData(
        salt=secrets.randbits(64), maker=ZERO, signer=ZERO, taker=ZERO, tokenId=int(token),
        makerAmount=collateral if side == "BUY" else size_micro,
        takerAmount=size_micro if side == "BUY" else collateral,
        expiration=0, nonce=0, feeRateBps=0, side=0 if side == "BUY" else 1, signatureType=0,
    )
    oid = OrderService._compute_order_id(order)
    _WRITER._insert_order(conn, api_key=api_key, order=order, order_id=oid, signature=b"\x00",
                          price_int=OrderService._price_int(order), order_type=order_type)
    return oid


def _take(db, svc, api_key, token, side, price_micro, size_micro, order_type="GTC"):
    with db.write() as conn:
        oid = _order(conn, api_key, token, side, price_micro, size_micro, order_type)
        return svc._match(conn, OrderService._get_order_row(conn, oid))


def _remaining(db, oid) -> int:
    with db.read() as conn:
        return int(conn.execute("SELECT REMAINING_AMOUNT FROM orders WHERE ORDER_ID = %s", (oid,)).fetchone()["REMAINING_AMOUNT"])


def _svc(db, **kw):
    return OrderService(db, None, Settings(**kw))  # type: ignore[arg-type]


def test_own_resting_order_is_not_matched():
    db = _db(); a = _account(db)
    with db.write() as conn:
        ask = _order(conn, a, YES, "SELL", 500_000, 10_000_000)
    assert _take(db, _svc(db), a, YES, "BUY", 600_000, 10_000_000) == []
    assert _remaining(db, ask) == 10_000_000


def test_another_account_still_matches():
    db = _db(); a = _account(db); b = _account(db)
    with db.write() as conn:
        _order(conn, a, YES, "SELL", 500_000, 10_000_000)
    assert len(_take(db, _svc(db), b, YES, "BUY", 600_000, 10_000_000)) == 1


def test_own_order_is_skipped_but_others_at_same_price_fill():
    db = _db(); a = _account(db); b = _account(db)
    with db.write() as conn:
        own = _order(conn, a, YES, "SELL", 500_000, 5_000_000)     # older, same price
        other = _order(conn, b, YES, "SELL", 500_000, 5_000_000)
    matches = _take(db, _svc(db), a, YES, "BUY", 500_000, 5_000_000)
    assert [m["maker_order_id"] for m in matches] == [other]
    assert _remaining(db, own) == 5_000_000


def test_owner_and_agents_are_one_family():
    db = _db()
    owner = _account(db, workos="user_fam")
    agent1 = _account(db, owner="user_fam")
    agent2 = _account(db, owner="user_fam")
    stranger = _account(db, owner="user_other")
    with db.write() as conn:
        _order(conn, owner, YES, "SELL", 500_000, 1_000_000)
        _order(conn, agent1, YES, "SELL", 500_000, 1_000_000)
    svc = _svc(db)
    assert _take(db, svc, agent2, YES, "BUY", 500_000, 2_000_000) == []     # agent vs sibling + owner
    assert _take(db, svc, owner, YES, "BUY", 500_000, 1_000_000) == []      # owner vs own agent
    assert len(_take(db, svc, stranger, YES, "BUY", 500_000, 2_000_000)) == 2


def test_mint_path_skips_own_complement_order():
    db = _db(); a = _account(db); b = _account(db)
    with db.write() as conn:
        _order(conn, a, YES, "BUY", 600_000, 1_000_000)              # resting BUY YES @0.6
    svc = _svc(db)
    assert _take(db, svc, a, NO, "BUY", 500_000, 1_000_000) == []  # own MINT skipped
    matches = _take(db, svc, b, NO, "BUY", 500_000, 1_000_000)
    assert len(matches) == 1 and matches[0]["match_kind"] == "MINT"


def test_maker_cap_stops_after_twenty():
    db = _db(); maker = _account(db); taker = _account(db)
    with db.write() as conn:
        asks = [_order(conn, maker, YES, "SELL", 500_000, 1_000_000) for _ in range(25)]
    matches = _take(db, _svc(db, AGENTPIT_MAX_MAKERS_PER_MATCH=20), taker, YES, "BUY", 500_000, 25_000_000)
    assert len(matches) == 20
    assert sum(_remaining(db, oid) for oid in asks) == 5_000_000


def test_fok_beyond_the_cap_is_killed():
    db = _db(); maker = _account(db); taker = _account(db)
    with db.write() as conn:
        for _ in range(25):
            _order(conn, maker, YES, "SELL", 500_000, 1_000_000)
    with pytest.raises(OrderNotFilledError):
        _take(db, _svc(db, AGENTPIT_MAX_MAKERS_PER_MATCH=20), taker, YES, "BUY", 500_000, 25_000_000, "FOK")
