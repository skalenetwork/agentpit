"""An activity page filters in SQL, so a filtered page reads no more of the
account's history than it shows."""

from agentpit.auth.passwords import hash_password
from agentpit.config import Settings
from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.market_state import MarketState
from agentpit.db.session import DbSession
from agentpit.db.table_write import TableWrite
from agentpit.services.account_service import AccountService
from tests.services.test_closed_positions import _hex32, _insert_trade


def _two_market_account() -> tuple[DbSession, str]:
    """Six trades alternating between two markets (market pa1 at t=2, 4, 6;
    pa2 at t=1, 3, 5) and one SPLIT on pa1."""
    db = DbSession(Settings().database_url)
    with db.write() as conn:
        _uid, acct, api_key = TableWrite.create_user(
            conn, email="pages@x.com", password_hash=hash_password("pw12pw12pw12"), handle=None
        )
        markets = {}
        for seed in ("pa1", "pa2"):
            req = CreateMarketRequest(
                question=f"{seed}?",
                description="d",
                erc1155_tokens=[(f"{seed}-y", "Yes"), (f"{seed}-n", "No")],
                slug=f"pages-{seed}",
                condition_id=ConditionId(_hex32(seed)),
                state=MarketState.ACTIVE,
            )
            markets[seed] = TableWrite.create_market(conn, req, is_polygon_market=False)
        for t in range(1, 7):
            seed = "pa1" if t % 2 == 0 else "pa2"
            _insert_trade(
                conn, asset=f"{seed}-y", price=400_000, size=1_000_000,
                market=_hex32(seed), taker_api_key=api_key, match_time=t,
            )
        TableWrite.log_transaction(conn, api_key, "SPLIT", markets["pa1"].market_id, {"amount": 2_000_000})
    return db, acct.address


def test_a_filtered_activity_page_reads_only_as_much_history_as_it_shows(monkeypatch):
    db, address = _two_market_account()
    asked: list[int | None] = []
    real = AccountService._trade_activity

    def spied(conn, api_key, eth_address, limit=None, **kwargs):
        asked.append(limit)
        return real(conn, api_key, eth_address, limit, **kwargs)

    monkeypatch.setattr(AccountService, "_trade_activity", staticmethod(spied))
    service = AccountService(db, onchain=None)  # type: ignore[arg-type]

    trades = service.list_activity(address, type_filter=["TRADE"], market=[_hex32("pa1")], limit=2)
    splits = service.list_activity(address, type_filter=["SPLIT"], market=[_hex32("pa1")])
    other = service.list_activity(address, market=[_hex32("pa2")])
    unfiltered = service.list_activity(address, market=[])

    assert [(a.type, a.timestamp) for a in trades] == [("TRADE", 6), ("TRADE", 4)]
    assert [(a.type, a.conditionId) for a in splits] == [("SPLIT", _hex32("pa1"))]
    assert [(a.type, a.conditionId, a.timestamp) for a in other] == [
        ("TRADE", _hex32("pa2"), 5),
        ("TRADE", _hex32("pa2"), 3),
        ("TRADE", _hex32("pa2"), 1),
    ]
    # An empty market list (`market=,`) is no filter, as it always was.
    assert len(unfiltered) == 7
    assert asked == [2, 100, 100]
