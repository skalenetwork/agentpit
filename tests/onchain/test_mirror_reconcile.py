# tests/onchain/test_mirror_reconcile.py
from decimal import Decimal

from agentpit.config import Settings
from agentpit.datastructures.place_order_request import PlaceOrderRequest
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.liquidity.feed import MarketRef
from agentpit.liquidity.house_accounts import HouseAccountProvisioner
from agentpit.liquidity.replica import BookSnapshot
from agentpit.liquidity.reconciler import reconcile_market
from agentpit.onchain.admin import OnchainAdmin
from agentpit.onchain.contracts import Contracts
from agentpit.onchain.deployment import Deployment
from agentpit.onchain.web3_client import Web3Client
from agentpit.services.order_service import OrderService
from tests.onchain._helpers import (
    HOUSE_TEST_GAS_FLOOR_WEI,
    create_market,
    fresh_client,
    register,
    unique_email,
)


def _rig():
    s = Settings(
        liquidity_house_account_count=1, liquidity_gas_floor_wei=HOUSE_TEST_GAS_FLOOR_WEI
    )
    d = Deployment.load(s.deployment_path)
    w = Web3Client(s, d)
    admin = OnchainAdmin(w, Contracts(w.web3, d))
    db = DbSession(s.database_url)
    client = fresh_client()
    m = create_market(client)
    cond = m["condition_id"]["value"]
    ref = MarketRef(
        market_id=m["market_id"], condition_id=cond,
        yes_token=m["erc1155_tokens"][0][0], no_token=m["erc1155_tokens"][1][0],
        pm_yes_token="PM-YES")
    user = HouseAccountProvisioner(db, admin, s).ensure_provisioned()[0]
    return s, db, admin, client, ref, user


def _snap(bids, asks):
    return BookSnapshot(asset_id="PM-YES", bids=tuple(bids), asks=tuple(asks))


def _levels(client, token):
    book = client.get(f"/book?token_id={token}").json()
    return ({float(b["price"]): float(b["size"]) for b in book["bids"]},
            {float(a["price"]): float(a["size"]) for a in book["asks"]})


def test_reconcile_mirrors_both_books_and_is_idempotent():
    s, db, admin, client, ref, user = _rig()
    order = OrderService(db, admin)
    snap = _snap(bids=[(400_000, 10_000_000), (300_000, 5_000_000)],
                 asks=[(600_000, 7_000_000)])

    stats = reconcile_market(db, order, admin, user, ref, snap, s)
    assert stats["placed"] == 6 and stats["cancelled"] == 0

    yes_bids, yes_asks = _levels(client, ref.yes_token)
    no_bids, no_asks = _levels(client, ref.no_token)
    assert yes_bids == {0.4: 10.0, 0.3: 5.0} and yes_asks == {0.6: 7.0}
    assert no_bids == {0.4: 7.0} and no_asks == {0.6: 10.0, 0.7: 5.0}

    stats2 = reconcile_market(db, order, admin, user, ref, snap, s)
    assert (stats2["placed"], stats2["cancelled"], stats2["fills"],
            stats2["deferred"], stats2["failed"]) == (0, 0, 0, 0, 0)

    with db.read() as conn:
        n = conn.execute("SELECT COUNT(*) AS C FROM trades WHERE MARKET = %s",
                         (ref.condition_id,)).fetchone()["C"]
    assert n == 0          # mirroring NEVER trades with itself


def test_reconcile_level_change_minimal_ops():
    s, db, admin, client, ref, user = _rig()
    order = OrderService(db, admin)
    reconcile_market(db, order, admin, user, ref,
                     _snap(bids=[(400_000, 10_000_000)], asks=[(600_000, 7_000_000)]), s)
    stats = reconcile_market(db, order, admin, user, ref,
                             _snap(bids=[(410_000, 10_000_000)],            # bid moved
                                   asks=[(600_000, 7_000_000)]), s)
    # Only the moved bid (YES BUY + NO SELL complement) is touched.
    assert stats["cancelled"] == 2 and stats["placed"] == 2
    yes_bids, _ = _levels(client, ref.yes_token)
    assert yes_bids == {0.41: 10.0}


def test_reconcile_fills_bot_order_when_price_passes_through(caplog):
    s, db, admin, client, ref, user = _rig()
    order = OrderService(db, admin)
    reconcile_market(db, order, admin, user, ref,
                     _snap(bids=[(400_000, 10_000_000)], asks=[(600_000, 7_000_000)]), s)

    # A real user rests a bid INSIDE the spread...
    email = unique_email()
    register(client, email)          # /register onboards + funds the account
    with db.read() as conn:
        bot = TableRead.get_user_by_email(conn, email)
    assert bot is not None
    bot_order = OrderService(db, admin).place_order(bot, PlaceOrderRequest(
        token_id=ref.yes_token, side="BUY",
        price=Decimal("0.55"), size=Decimal("2"), order_type="GTC"))
    assert bot_order.success and not bot_order.tradeIDs

    # ...and the real market moves DOWN through it: new ask 0.50 < bot bid 0.55.
    # BOTH the direct SELL YES @0.50 (NORMAL cross) and the complement
    # BUY NO @0.50 (MINT cross via 1-p) must be classified HOT; with the
    # default budget of 1 settlement/cycle, one fills and one is deferred.
    with caplog.at_level("ERROR", logger="agentpit.liquidity.reconciler"):
        stats = reconcile_market(db, order, admin, user, ref,
                                 _snap(bids=[(400_000, 10_000_000)],
                                       asks=[(500_000, 7_000_000)]), s)
    assert stats["fills"] == 1
    assert stats["deferred"] == 1
    assert not [r for r in caplog.records if r.levelname == "ERROR"], \
        "no 'guard missed' ERROR may fire — both crossers must be classified hot"

    with db.read() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS C FROM trades WHERE MARKET = %s AND STATUS != 'FAILED'",
            (ref.condition_id,)).fetchone()["C"]
    assert n >= 1

    # The deferred crossing placement converges on the next cycle.
    stats2 = reconcile_market(db, order, admin, user, ref,
                              _snap(bids=[(400_000, 10_000_000)],
                                    asks=[(500_000, 7_000_000)]), s)
    assert stats2["deferred"] == 0


def test_place_resting_orders_batch_inserts_without_matching():
    _s, db, admin, client, ref, user = _rig()
    order = OrderService(db, admin)
    # Non-crossing BUYs (apUSD-backed); a single transaction, no matching.
    reqs = [
        PlaceOrderRequest(token_id=ref.yes_token, side="BUY",
                          price=Decimal("0.40"), size=Decimal("10"), order_type="GTC"),
        PlaceOrderRequest(token_id=ref.yes_token, side="BUY",
                          price=Decimal("0.30"), size=Decimal("5"), order_type="GTC"),
    ]
    ids = order.place_resting_orders(user, reqs)
    assert len(ids) == 2
    yes_bids, yes_asks = _levels(client, ref.yes_token)
    assert yes_bids == {0.4: 10.0, 0.3: 5.0} and yes_asks == {}
    with db.read() as conn:
        n = conn.execute("SELECT COUNT(*) AS C FROM trades WHERE MARKET = %s",
                         (ref.condition_id,)).fetchone()["C"]
    assert n == 0          # pure resting insert — no matching/settlement


def test_place_resting_orders_skips_underfunded():
    _s, db, admin, client, ref, user = _rig()
    order = OrderService(db, admin)
    # A SELL needs CTF inventory the freshly-provisioned house doesn't hold yet,
    # so its balance check fails and it's omitted — no exception, no partial book.
    reqs = [PlaceOrderRequest(token_id=ref.yes_token, side="SELL",
                              price=Decimal("0.60"), size=Decimal("7"), order_type="GTC")]
    ids = order.place_resting_orders(user, reqs)
    assert ids == []
    _, yes_asks = _levels(client, ref.yes_token)
    assert yes_asks == {}


def test_replace_resting_orders_cancels_and_places_atomically():
    _s, db, admin, client, ref, user = _rig()
    order = OrderService(db, admin)
    first = order.place_resting_orders(user, [
        PlaceOrderRequest(token_id=ref.yes_token, side="BUY",
                          price=Decimal("0.40"), size=Decimal("10"), order_type="GTC"),
    ])
    assert len(first) == 1
    # Cancel the old bid and place a new one at a different level, atomically.
    ids = order.replace_resting_orders(user, first, [
        PlaceOrderRequest(token_id=ref.yes_token, side="BUY",
                          price=Decimal("0.41"), size=Decimal("10"), order_type="GTC"),
    ])
    assert len(ids) == 1
    yes_bids, _ = _levels(client, ref.yes_token)
    assert yes_bids == {0.41: 10.0}   # old 0.40 cancelled, new 0.41 live — only one
