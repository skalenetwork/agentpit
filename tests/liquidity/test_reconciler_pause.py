"""While the admin gas breaker is paused every hot placement is refused, and the
reconciler runs every 0.5 s: a traceback per refusal would bury the one ERROR
the balance loop writes for the same cause."""
import logging

from agentpit.config import Settings
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import AdminGasPausedError
from agentpit.liquidity.reconciler import reconcile_market
from agentpit.liquidity.replica import BookSnapshot
from tests.db_helpers import fresh_test_db


class _Ref:
    market_id = 1
    condition_id = "0x" + "ab" * 32
    yes_token = "111"
    no_token = "222"


class _Chain:
    """Holds plenty of everything, so no inventory split is wanted."""

    def ctf_balance(self, _addr, _tok):
        return 10**12

    def usd_balance(self, _addr):
        return 10**12


class _PausedOrders:
    def __init__(self):
        self.place_calls = 0

    def replace_resting_orders(self, _user, _cancels, _reqs, balance_hints=None):
        return []

    def place_order(self, _user, _req, balance_hint=None):
        self.place_calls += 1
        raise AdminGasPausedError()


def test_hot_placements_refused_by_the_breaker_fail_quietly(caplog):
    db = fresh_test_db()
    with db.write() as conn:
        house_id, _acct, _key = TableWrite.create_user(conn, email="house@example.com", password_hash=None, handle=None)
        _other_id, _acct, other_key = TableWrite.create_user(conn, email="other@example.com", password_hash=None, handle=None)
        # A real user's resting ask the house's bid at 0.60 crosses: a hot placement.
        conn.execute(
            "INSERT INTO orders (ORDER_ID, TOKEN_ID, SIDE, PRICE, STATUS, REMAINING_AMOUNT, EXPIRATION, CREATED_AT, API_KEY, ORDER_TYPE) "
            "VALUES ('0xforeign', %s, 'SELL', 500000, 'live', 1000000, 0, 0, %s, 'GTC')",
            (_Ref.yes_token, other_key),
        )
        house = TableRead.get_user_by_userid(conn, house_id)
    snap = BookSnapshot(asset_id="PM-YES", bids=((600_000, 10_000_000),), asks=())
    orders = _PausedOrders()

    with caplog.at_level(logging.DEBUG, logger="agentpit.liquidity.reconciler"):
        stats = reconcile_market(db, orders, _Chain(), house, _Ref(), snap, Settings())  # type: ignore[arg-type]

    assert orders.place_calls >= 1 and stats["failed"] == orders.place_calls
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], "no warning, let alone a traceback"
    assert any("admin gas breaker" in r.getMessage() for r in caplog.records)
