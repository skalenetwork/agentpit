import json
import time
from dataclasses import replace
from types import SimpleNamespace

from agentpit.datastructures.match import Match
from agentpit.db.session import DbSession
from agentpit.onchain.order_signer import OrderData
from agentpit.services import order_service
from agentpit.services.order_service import OrderService
from tests.db_helpers import fresh_test_db
from tests.fake_skaled import FakeFn, FakeSkaled, make_sender

_ADDR = "0x00000000000000000000000000000000000000a1"
_SIG = b"\x01" * 65


def _order(side: int) -> OrderData:
    return OrderData(
        salt=1, maker=_ADDR, signer=_ADDR, taker=_ADDR, tokenId=1,
        makerAmount=40, takerAmount=100, expiration=0, nonce=0, feeRateBps=0,
        side=side, signatureType=0,
    )


def _service(sender) -> tuple[OrderService, DbSession]:
    db = fresh_test_db()
    exchange = SimpleNamespace(functions=SimpleNamespace(matchOrders=lambda *_: FakeFn()))
    onchain = SimpleNamespace(
        _client=SimpleNamespace(admin_sender=sender, web3=None),
        _contracts=SimpleNamespace(exchange=exchange),
    )
    return OrderService(db, onchain), db  # type: ignore[arg-type]


def _trade(db: DbSession, trade_id: str, matched_at: int = 0, tx_hash: str | None = None) -> None:
    with db.write() as conn:
        conn.execute(
            "INSERT INTO trades (TRADE_ID, STATUS, MATCH_TIME, TRANSACTION_HASH) "
            "VALUES (%s, 'PENDING', %s, %s)",
            (trade_id, matched_at, tx_hash),
        )


def _match(db: DbSession, trade_id: str) -> Match:
    _trade(db, trade_id)
    return Match(
        takes=(),
        size=100,
        house_amount=60,
        agent_amount=40,
        kind="MINT",
        house_order=_order(0),
        house_signature=_SIG,
        trade_id=trade_id,
    )


def _trades(db: DbSession) -> dict[str, tuple[str, str | None]]:
    with db.read() as conn:
        rows = conn.execute("SELECT TRADE_ID, STATUS, TRANSACTION_HASH FROM trades").fetchall()
    return {r["TRADE_ID"]: (r["STATUS"], r["TRANSACTION_HASH"]) for r in rows}


def test_a_reverted_fill_fails():
    chain = FakeSkaled()
    chain.revert = {0}
    sender, _, _ = make_sender(chain)
    svc, db = _service(sender)

    error, tx_hash = svc._settle(_order(0), _SIG, _match(db, "t-mint"), wait=True)

    assert _trades(db)["t-mint"] == ("FAILED", tx_hash)
    assert error == f"settlement failed: {tx_hash} reverted"


def test_a_sweeper_fill_returns_once_it_is_pending():
    chain = FakeSkaled()
    sender, _, _ = make_sender(chain)
    svc, db = _service(sender)

    error, tx_hash = svc._settle(_order(0), _SIG, _match(db, "t-swept"), wait=False)

    assert error == "" and tx_hash
    assert _trades(db)["t-swept"] == ("PENDING", tx_hash)


def test_an_unknown_outcome_stays_pending_until_the_cleanup_reads_it(monkeypatch):
    chain = FakeSkaled()
    sender, _, _ = make_sender(chain, mine_on_sleep=False)
    svc, db = _service(sender)
    now = int(time.time())

    assert svc._settle(_order(0), _SIG, _match(db, "t-landed"), wait=True)[0] == ""
    status, tx_hash = _trades(db)["t-landed"]
    assert status == "PENDING" and tx_hash

    chain.mine()
    _trade(db, "t-lost", tx_hash="0x" + "ab" * 32)
    _trade(db, "t-recent", matched_at=now - 120, tx_hash="0x" + "cd" * 32)
    monkeypatch.setattr(order_service, "Web3ChainRpc", lambda _web3: chain)
    svc.settle_pending(now)

    assert {k: v[0] for k, v in _trades(db).items()} == {
        "t-landed": "CONFIRMED",
        "t-lost": "FAILED",
        "t-recent": "PENDING",
    }


def test_a_fill_that_left_no_hash_settles_by_its_house_order():
    svc, db = _service(None)
    functions = svc._onchain._contracts.exchange.functions
    functions.hashOrder = lambda order: SimpleNamespace(call=lambda: order[0])
    functions.orderStatus = lambda salt: SimpleNamespace(call=lambda: (salt == 1, 0))
    now = int(time.time())
    for trade_id, salt, matched_at in (
        ("t-landed", 1, 0),
        ("t-lost", 2, 0),
        ("t-recent", 2, now - 120),
    ):
        with db.write() as conn:
            svc._insert_order(
                conn,
                api_key="house",
                order=replace(_order(0), salt=salt),
                order_id=f"house-{trade_id}",
                signature=_SIG,
                price_int=600_000,
                order_type="FOK",
            )
            conn.execute(
                "INSERT INTO trades (TRADE_ID, STATUS, MATCH_TIME, MAKER_ORDERS) "
                "VALUES (%s, 'PENDING', %s, %s)",
                (trade_id, matched_at, json.dumps([{"order_id": f"house-{trade_id}"}])),
            )

    svc.settle_pending(now)

    assert _trades(db) == {
        "t-landed": ("CONFIRMED", None),
        "t-lost": ("FAILED", None),
        "t-recent": ("PENDING", None),
    }
