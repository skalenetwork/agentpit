"""list_closed_positions reconstructs won positions from REDEEM payouts (the
Active list drops them once the tokens are redeemed). Payout is deduped per
market; entry/PnL come from the user's fills on the winning token."""

import json
import uuid

from agentpit.auth.passwords import hash_password
from agentpit.config import Settings
from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.market_state import MarketState
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.services.account_service import AccountService


def _hex32(seed: str) -> str:
    return "0x" + seed.encode().hex().ljust(64, "0")[:64]


def _insert_trade(
    conn,
    *,
    asset,
    price,
    size,
    taker_api_key,
    side="BUY",
    maker_side="SELL",
    match_time=0,
    market=None,
):
    """Insert one fill with the user as TAKER.

    `maker_side` is what the matched maker order did. It matters: the pair
    (taker BUY, maker BUY) is a MINT, where the stored PRICE is the maker's
    complement rather than what the taker paid. Every other combination stores
    the price both sides transacted at.

    `market` is the condition id, which production always sets. It is what
    `list_markets_with_user_activity` matches on, so a fill without it is
    invisible to any reconstruction that starts from the user's markets.
    """
    conn.execute(
        "INSERT INTO trades (TRADE_ID, MARKET, ASSET_ID, SIDE, PRICE, TRADE_SIZE, "
        "STATUS, TAKER_API_KEY, MAKER_ORDERS, MATCH_TIME) "
        "VALUES (%s, %s, %s, %s, %s, %s, 'CONFIRMED', %s, %s, %s)",
        (
            uuid.uuid4().hex,
            market,
            asset,
            side,
            price,
            size,
            taker_api_key,
            json.dumps([{"side": maker_side}]),
            match_time,
        ),
    )


def test_list_closed_positions_redeem_driven_dedup_and_pnl():
    db = DbSession(Settings().database_url)
    with db.write() as conn:
        _uid, acct, api_key = TableWrite.create_user(
            conn,
            email="closed@x.com",
            password_hash=hash_password("pw12pw12pw12"),
            handle=None,
        )
        req = CreateMarketRequest(
            question="Win?",
            description="d",
            erc1155_tokens=[("yt", "Yes"), ("nt", "No")],
            slug="win-q",
            condition_id=ConditionId(_hex32("c1")),
            state=MarketState.ACTIVE,
        )
        m = TableWrite.create_market(conn, req, is_polygon_market=False)
        TableWrite.set_market_state(conn, m.market_id, MarketState.ACTIVE, MarketState.RESOLVED, (1, 0))
        # Bought 100 YES @ 0.40 (the winning side).
        _insert_trade(conn, asset="yt", price=400_000, size=100_000_000,
                      taker_api_key=api_key)
        # Two identical REDEEM logs -> deduped to ONE $100 payout (not $200).
        for _ in range(2):
            TableWrite.log_transaction(
                conn, api_key, "REDEEM", m.market_id,
                {"collateral_amount": 100_000_000},
            )

    out = AccountService(db, onchain=None).list_closed_positions(  # type: ignore[arg-type]
        acct.address
    )
    assert len(out) == 1
    p = out[0]
    assert p.outcome == "Yes"
    assert abs(p.currentValue - 100.0) < 1e-6  # deduped payout, not 200
    assert abs(p.size - 100.0) < 1e-6
    assert abs(p.avgPrice - 0.40) < 1e-6
    assert abs(p.cashPnl - 60.0) < 1e-6  # 100 payout - 40 cost


def test_list_closed_positions_includes_losses():
    db = DbSession(Settings().database_url)
    with db.write() as conn:
        _uid, acct, api_key = TableWrite.create_user(
            conn,
            email="loss@x.com",
            password_hash=hash_password("pw12pw12pw12"),
            handle=None,
        )
        req = CreateMarketRequest(
            question="Lose?",
            description="d",
            erc1155_tokens=[("yt2", "Yes"), ("nt2", "No")],
            slug="lose-q",
            condition_id=ConditionId(_hex32("c2")),
            state=MarketState.ACTIVE,
        )
        m = TableWrite.create_market(conn, req, is_polygon_market=False)
        # YES (idx 0) won; the user bought NO — the losing side.
        TableWrite.set_market_state(conn, m.market_id, MarketState.ACTIVE, MarketState.RESOLVED, (1, 0))
        _insert_trade(conn, asset="nt2", price=500_000, size=50_000_000,
                      taker_api_key=api_key)  # 50 NO @ 0.50
        TableWrite.log_transaction(
            conn, api_key, "REDEEM", m.market_id, {"collateral_amount": 0}
        )

    out = AccountService(db, onchain=None).list_closed_positions(  # type: ignore[arg-type]
        acct.address
    )
    assert len(out) == 1
    p = out[0]
    assert p.outcome == "No"  # the losing outcome the user held
    assert abs(p.currentValue - 0.0) < 1e-6  # lost -> $0
    assert abs(p.size - 50.0) < 1e-6
    assert abs(p.avgPrice - 0.50) < 1e-6
    assert abs(p.cashPnl + 25.0) < 1e-6  # loss = -cost (0.50 * 50)


# ----- positions closed by SELLING out ----------------------------------------
#
# A position exited by selling produces no REDEEM and leaves no token balance,
# so before this it fell between the two lists: /positions drops it (size 0) and
# the redeem-driven reconstruction above never saw it. It vanished from the
# Closed tab, the predictions count, Biggest Win and the P/L chart at once.


def _sell_closed_fixture(email, seed, token, *, buys, sells, resolve=None):
    """One user who bought and then sold `token`. Returns (db, address).

    `buys`/`sells` are (price_micro, size_micro, match_time) tuples.
    """
    db = DbSession(Settings().database_url)
    with db.write() as conn:
        _uid, acct, api_key = TableWrite.create_user(
            conn,
            email=email,
            password_hash=hash_password("pw12pw12pw12"),
            handle=None,
        )
        req = CreateMarketRequest(
            question="Sold out?",
            description="d",
            erc1155_tokens=[(token, "Yes"), (f"{token}-no", "No")],
            slug=f"sold-{seed}",
            condition_id=ConditionId(_hex32(seed)),
            state=MarketState.ACTIVE,
        )
        m = TableWrite.create_market(conn, req, is_polygon_market=False)
        if resolve is not None:
            TableWrite.set_market_state(
                conn,
                m.market_id,
                MarketState.ACTIVE,
                MarketState.RESOLVED,
                (int(resolve == 0), int(resolve == 1)),
            )
        for price, size, t in buys:
            _insert_trade(
                conn, asset=token, price=price, size=size, market=_hex32(seed),
                taker_api_key=api_key, side="BUY", maker_side="SELL", match_time=t,
            )
        for price, size, t in sells:
            # A taker SELL is matched by a maker BUY. This is NOT a mint, even
            # though `maker_side` reads BUY in both cases.
            _insert_trade(
                conn, asset=token, price=price, size=size, market=_hex32(seed),
                taker_api_key=api_key, side="SELL", maker_side="BUY", match_time=t,
            )
    return db, acct.address, m


def test_a_split_redeem_shows_the_side_the_user_held():
    db = DbSession(Settings().database_url)
    with db.write() as conn:
        _uid, acct, api_key = TableWrite.create_user(
            conn,
            email="split@x.com",
            password_hash=hash_password("pw12pw12pw12"),
            handle=None,
        )
        req = CreateMarketRequest(
            question="Split?",
            description="d",
            erc1155_tokens=[("ys", "Yes"), ("ns", "No")],
            slug="split-q",
            condition_id=ConditionId(_hex32("c-split")),
            state=MarketState.ACTIVE,
        )
        m = TableWrite.create_market(conn, req, is_polygon_market=False)
        TableWrite.set_market_state(conn, m.market_id, MarketState.ACTIVE, MarketState.RESOLVED, (1, 1))
        _insert_trade(conn, asset="ns", price=300_000, size=40_000_000,
                      taker_api_key=api_key, market=m.condition_id.value)
        TableWrite.log_transaction(
            conn, api_key, "REDEEM", m.market_id, {"collateral_amount": 20_000_000}
        )

    (p,) = AccountService(db, onchain=None).list_closed_positions(  # type: ignore[arg-type]
        acct.address
    )
    assert p.outcome == "No"
    assert abs(p.avgPrice - 0.30) < 1e-6
    assert abs(p.size - 40.0) < 1e-6


def test_sold_out_position_appears_in_closed_with_realized_pnl():
    """The exact production scenario: bought 20 @ 0.48, sold 20 @ 0.51."""
    db, address, _m = _sell_closed_fixture(
        "sellout@x.com", "s1", "st1",
        buys=[(480_000, 10_000_000, 100), (480_000, 10_000_000, 200)],
        sells=[(510_000, 20_000_000, 300)],
    )
    out = AccountService(db, onchain=None).list_closed_positions(  # type: ignore[arg-type]
        address
    )
    assert len(out) == 1
    p = out[0]
    assert p.outcome == "Yes"
    assert abs(p.size - 20.0) < 1e-6
    assert abs(p.avgPrice - 0.48) < 1e-6
    assert abs(p.initialValue - 9.60) < 1e-6   # cost
    assert abs(p.currentValue - 10.20) < 1e-6  # sale proceeds
    assert abs(p.cashPnl - 0.60) < 1e-6
    assert abs(p.percentPnl - 6.25) < 1e-6


def test_sold_out_position_does_not_apply_the_mint_price_flip():
    """The trap. `_avg_fill_price` flips the recorded price when the maker side
    is BUY, which is correct ONLY because it pre-filters to taker BUYs — a mint
    is (taker BUY, maker BUY). A taker SELL is also matched by a maker BUY, and
    flipping there would book the 0.51 sale at 0.49 and turn a $0.60 profit into
    a $0.20 loss."""
    db, address, _m = _sell_closed_fixture(
        "mintflip@x.com", "s2", "st2",
        buys=[(480_000, 20_000_000, 100)],
        sells=[(510_000, 20_000_000, 300)],
    )
    p = AccountService(db, onchain=None).list_closed_positions(address)[0]  # type: ignore[arg-type]
    assert abs(p.currentValue - 10.20) < 1e-6, "sale booked at the complement price"
    assert p.cashPnl > 0


def test_sold_out_position_populates_the_realized_pnl_fields():
    db, address, _m = _sell_closed_fixture(
        "realized@x.com", "s3", "st3",
        buys=[(480_000, 20_000_000, 100)],
        sells=[(510_000, 20_000_000, 300)],
    )
    p = AccountService(db, onchain=None).list_closed_positions(address)[0]  # type: ignore[arg-type]
    assert abs(p.realizedPnl - 0.60) < 1e-6
    assert abs(p.percentRealizedPnl - 6.25) < 1e-6


def test_sold_out_at_a_loss_is_reported_as_a_loss():
    db, address, _m = _sell_closed_fixture(
        "sellloss@x.com", "s4", "st4",
        buys=[(500_000, 20_000_000, 100)],
        sells=[(400_000, 20_000_000, 300)],
    )
    p = AccountService(db, onchain=None).list_closed_positions(address)[0]  # type: ignore[arg-type]
    assert abs(p.cashPnl + 2.0) < 1e-6      # 8.00 proceeds - 10.00 cost
    assert abs(p.percentPnl + 20.0) < 1e-6


def test_end_date_is_the_last_sell_not_the_market_end():
    """The P/L chart plots each closed position at its `endDate`. For a sold
    position the realization happened at the sale; the market's own end date is
    still in the future and would plot the profit ahead of today."""
    db, address, _m = _sell_closed_fixture(
        "selltime@x.com", "s5", "st5",
        buys=[(480_000, 20_000_000, 100)],
        sells=[(500_000, 10_000_000, 300), (520_000, 10_000_000, 450)],
    )
    p = AccountService(db, onchain=None).list_closed_positions(address)[0]  # type: ignore[arg-type]
    assert p.endDate == "450"


def test_partially_sold_position_is_not_closed():
    """Still holding 5 of 20 — it belongs in Active, not Closed."""
    db, address, _m = _sell_closed_fixture(
        "partial@x.com", "s6", "st6",
        buys=[(480_000, 20_000_000, 100)],
        sells=[(510_000, 15_000_000, 300)],
    )
    assert AccountService(db, onchain=None).list_closed_positions(address) == []  # type: ignore[arg-type]


def test_never_sold_position_is_not_closed():
    db, address, _m = _sell_closed_fixture(
        "held@x.com", "s7", "st7",
        buys=[(480_000, 20_000_000, 100)],
        sells=[],
    )
    assert AccountService(db, onchain=None).list_closed_positions(address) == []  # type: ignore[arg-type]


def test_sold_out_market_is_not_duplicated_by_the_redeem_path():
    """Sold out AND resolved: the redeem reconstruction owns the market, so the
    sell reconstruction must not emit a second row for it."""
    db, address, m = _sell_closed_fixture(
        "both@x.com", "s8", "st8",
        buys=[(480_000, 20_000_000, 100)],
        sells=[(510_000, 20_000_000, 300)],
        resolve=0,
    )
    with db.write() as conn:
        user = TableRead.get_user_by_eth_address(conn, address)
        assert user is not None
        TableWrite.log_transaction(
            conn, user.api_key, "REDEEM", m.market_id,
            {"collateral_amount": 5_000_000},
        )
    out = AccountService(db, onchain=None).list_closed_positions(address)  # type: ignore[arg-type]
    assert len(out) == 1


def test_sold_out_position_carries_the_event_slug():
    """The profile links a position at the EVENT that groups it, not at the bare
    market — a Fed market lives inside "Fed Decision in September?" alongside its
    sibling outcomes, and that page is what the user means to open."""
    db, address, m = _sell_closed_fixture(
        "eventslug@x.com", "s9", "st9",
        buys=[(480_000, 20_000_000, 100)],
        sells=[(510_000, 20_000_000, 300)],
    )
    with db.write() as conn:
        event = TableWrite.upsert_event(
            conn, slug="fed-decision-in-september", title="Fed Decision in September?"
        )
        TableWrite.attach_market_to_event(
            conn, market_id=m.market_id, event_id=event.event_id
        )
    p = AccountService(db, onchain=None).list_closed_positions(address)[0]  # type: ignore[arg-type]
    assert p.eventSlug == "fed-decision-in-september"


def test_sold_out_position_without_an_event_leaves_the_slug_empty():
    """An unattached market must yield "" rather than a broken /events/ link."""
    db, address, _m = _sell_closed_fixture(
        "noevent@x.com", "s10", "st10",
        buys=[(480_000, 20_000_000, 100)],
        sells=[(510_000, 20_000_000, 300)],
    )
    p = AccountService(db, onchain=None).list_closed_positions(address)[0]  # type: ignore[arg-type]
    assert p.eventSlug == ""
