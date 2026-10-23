"""list_bets: one bet per outcome the account bought, from its fills, its
REDEEM payouts and the positions the valuation pass read."""

import json
import uuid

import psycopg

from agentpit.datastructures.bet import Bet, BetFill, PriceLine
from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.market import Market
from agentpit.datastructures.market_state import MarketState
from agentpit.datastructures.position_wire import PositionWire
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.services.account_service import AccountService
from tests.db_helpers import fresh_test_conn, fresh_test_db

H = 3_600
T0 = 500_000 * H
NOW = T0 + 10 * H


def _hex32(seed: str) -> str:
    return "0x" + seed.encode().hex().ljust(64, "0")[:64]


def _account(conn) -> tuple[str, str]:
    _uid, acct, key = TableWrite.create_user(conn, email=f"{uuid.uuid4().hex}@x.com", password_hash="x")
    return acct.address, key


def _market(
    conn, seed: str, *, payouts: tuple[int, int] | None = None, resolved_at: int | None = None, end: int | None = None
) -> Market:
    m = TableWrite.create_market(
        conn,
        CreateMarketRequest(
            question=f"{seed}?",
            description="d",
            erc1155_tokens=[(f"{seed}-y", "Yes"), (f"{seed}-n", "No")],
            slug=seed,
            condition_id=ConditionId(_hex32(seed)),
            state=MarketState.ACTIVE,
            start_date=1,
            end_date=end,
        ),
        is_polygon_market=False,
    )
    if payouts is not None:
        TableWrite.set_market_state(conn, m.market_id, MarketState.ACTIVE, MarketState.RESOLVED, payouts)
        conn.execute("UPDATE markets SET RESOLVED_AT = %s WHERE MARKET_ID = %s", (resolved_at, m.market_id))
    return m


def _fill(conn, key: str, m: Market, token: str, side: str, price: int, size: int, at: int, tx: str = "") -> None:
    conn.execute(
        "INSERT INTO trades (TRADE_ID, MARKET, ASSET_ID, SIDE, PRICE, TRADE_SIZE, MATCH_KIND, STATUS, "
        "TAKER_API_KEY, MATCH_TIME, TRANSACTION_HASH) VALUES (%s, %s, %s, %s, %s, %s, 'NORMAL', 'CONFIRMED', %s, %s, %s)",
        (uuid.uuid4().hex, m.condition_id.value, token, side, price, size, key, at, tx),
    )


def _redeem(conn, key: str, m: Market, amount: int, at: int) -> None:
    conn.execute(
        "INSERT INTO transactions (TIMESTAMP, API_KEY, TRANSACTION_TYPE, MARKET_ID, DETAILS) "
        "VALUES (to_timestamp(%s), %s, 'REDEEM', %s, %s)",
        (at, key, m.market_id, json.dumps({"collateral_amount": amount})),
    )


def _print(conn, token: str, price: int, at: int, *, other: str | None = None) -> None:
    """`token` prints at `price`, or at 1 - `price` through a print of `other`."""
    conn.execute(
        "INSERT INTO trades (TRADE_ID, ASSET_ID, MAKER_ASSET_ID, SIDE, PRICE, TRADE_SIZE, STATUS, MATCH_TIME) "
        "VALUES (%s, %s, %s, 'BUY', %s, 1000000, 'MIRRORED', %s)",
        (uuid.uuid4().hex, other or token, token if other else None, price, at),
    )


def _bets(address: str, positions: list[PositionWire] | None = None, previous: list[Bet] | None = None, previous_at: int = 0) -> list[Bet]:
    return AccountService(fresh_test_db(), onchain=None).list_bets(address, positions or [], NOW, previous or [], previous_at)


def _by_outcome(bets: list[Bet]) -> dict[str, Bet]:
    return {b.outcome: b for b in bets}


def test_an_open_bet_is_marked_by_its_position():
    conn = fresh_test_conn()
    address, key = _account(conn)
    m = _market(conn, "open")
    _fill(conn, key, m, "open-y", "BUY", 400_000, 100_000_000, T0, tx="0xabc")
    conn.close()
    held = PositionWire(asset="open-y", size=100.0, curPrice=0.55, currentValue=55.0, sellableValue=52.5)
    (bet,) = _bets(address, [held])
    assert (bet.status, bet.closedAt, bet.exit, bet.held, bet.entry, bet.shares, bet.sold) == ("open", None, 0.55, 100.0, 0.4, 100.0, 0.0)
    assert (bet.cost, bet.back, bet.value, bet.sellsFor, bet.pnl) == ("40000000", "0", "55000000", "52500000", "15000000")
    assert (bet.id, bet.conditionId, bet.title, bet.openedAt, bet.buys, bet.sells) == ("open-y", m.condition_id.value, "open?", T0, 1, 0)
    assert bet.fills == [BetFill(at=T0, side="BUY", shares=100.0, price=0.4, dollars="40000000", tx="0xabc")]


def test_a_won_bet_is_paid_out_once_at_its_redeem():
    conn = fresh_test_conn()
    address, key = _account(conn)
    m = _market(conn, "won", payouts=(1, 0), resolved_at=T0 + H)
    _fill(conn, key, m, "won-y", "BUY", 400_000, 100_000_000, T0)
    _redeem(conn, key, m, 100_000_000, T0 + 2 * H)
    conn.close()
    (bet,) = _bets(address)
    assert (bet.status, bet.exit, bet.closedAt, bet.held) == ("won", 1.0, T0 + 2 * H, 0.0)
    assert (bet.back, bet.value, bet.pnl) == ("100000000", "0", "60000000")
    assert bet.fills[0] == BetFill(at=T0 + 2 * H, side="REDEEM", shares=100.0, price=None, dollars="100000000", tx=None)
    assert [f.side for f in bet.fills] == ["REDEEM", "BUY"]


def test_a_won_bet_not_yet_redeemed_is_worth_its_payout():
    conn = fresh_test_conn()
    address, key = _account(conn)
    m = _market(conn, "unpaid", payouts=(1, 0), resolved_at=T0 + H)
    _fill(conn, key, m, "unpaid-y", "BUY", 400_000, 100_000_000, T0)
    conn.close()
    settled = PositionWire(asset="unpaid-y", size=100.0, curPrice=1.0, currentValue=100.0, sellableValue=0.0, settled=True)
    (bet,) = _bets(address, [settled])
    assert (bet.status, bet.closedAt, bet.held) == ("won", T0 + H, 100.0)
    assert (bet.back, bet.value, bet.sellsFor, bet.pnl) == ("0", "100000000", "0", "60000000")
    assert [f.side for f in bet.fills] == ["BUY"]


def test_a_lost_bet_closes_at_resolution_or_else_at_the_market_end():
    conn = fresh_test_conn()
    address, key = _account(conn)
    timed = _market(conn, "timed", payouts=(0, 1), resolved_at=T0 + 3 * H, end=T0 + H)
    untimed = _market(conn, "untimed", payouts=(0, 1), end=T0 + 2 * H)
    _fill(conn, key, timed, "timed-y", "BUY", 300_000, 50_000_000, T0)
    _fill(conn, key, untimed, "untimed-y", "BUY", 300_000, 50_000_000, T0)
    _redeem(conn, key, timed, 0, T0 + 4 * H)
    conn.close()
    bets = {b.id: b for b in _bets(address)}
    assert [(b.status, b.closedAt, b.exit, b.pnl) for b in bets.values()] == [
        ("lost", T0 + 3 * H, 0.0, "-15000000"),
        ("lost", T0 + 2 * H, 0.0, "-15000000"),
    ]
    assert all(f.side == "BUY" for b in bets.values() for f in b.fills)


def test_a_bet_sold_out_before_resolution_stays_sold():
    conn = fresh_test_conn()
    address, key = _account(conn)
    m = _market(conn, "sold", payouts=(1, 0), resolved_at=T0 + 5 * H)
    _fill(conn, key, m, "sold-y", "BUY", 480_000, 20_000_000, T0)
    _fill(conn, key, m, "sold-y", "SELL", 500_000, 10_000_000, T0 + H)
    _fill(conn, key, m, "sold-y", "SELL", 520_000, 10_000_000, T0 + 2 * H)
    conn.close()
    (bet,) = _bets(address)
    assert (bet.status, bet.exit, bet.closedAt, bet.sold, bet.buys, bet.sells) == ("sold", 0.51, T0 + 2 * H, 20.0, 1, 2)
    assert (bet.cost, bet.back, bet.pnl) == ("9600000", "10200000", "600000")
    assert [f.side for f in bet.fills] == ["SELL", "SELL", "BUY"]


def test_a_partial_sell_takes_its_result_at_resolution():
    conn = fresh_test_conn()
    address, key = _account(conn)
    m = _market(conn, "partial", payouts=(1, 0), resolved_at=T0 + 3 * H)
    _fill(conn, key, m, "partial-y", "BUY", 400_000, 100_000_000, T0)
    _fill(conn, key, m, "partial-y", "SELL", 700_000, 40_000_000, T0 + H)
    _redeem(conn, key, m, 60_000_000, T0 + 4 * H)
    conn.close()
    (bet,) = _bets(address)
    assert (bet.status, bet.exit, bet.sold, bet.closedAt) == ("won", 1.0, 40.0, T0 + 4 * H)
    assert (bet.cost, bet.back, bet.pnl) == ("40000000", "88000000", "48000000")
    assert bet.fills[0].shares == 60.0


def test_both_sides_of_one_market_are_two_bets():
    conn = fresh_test_conn()
    address, key = _account(conn)
    m = _market(conn, "both", payouts=(1, 0), resolved_at=T0 + H)
    _fill(conn, key, m, "both-y", "BUY", 600_000, 100_000_000, T0)
    _fill(conn, key, m, "both-n", "BUY", 300_000, 50_000_000, T0 + 60)
    _redeem(conn, key, m, 100_000_000, T0 + 2 * H)
    conn.close()
    bets = _by_outcome(_bets(address))
    assert (bets["Yes"].status, bets["Yes"].back, bets["Yes"].pnl) == ("won", "100000000", "40000000")
    assert (bets["No"].status, bets["No"].back, bets["No"].pnl) == ("lost", "0", "-15000000")
    assert [f.side for f in bets["No"].fills] == ["BUY"]


def test_a_50_50_split_shares_the_collateral_by_weight():
    conn = fresh_test_conn()
    address, key = _account(conn)
    m = _market(conn, "split", payouts=(1, 1), resolved_at=T0 + H)
    _fill(conn, key, m, "split-y", "BUY", 500_000, 30_000_000, T0)
    _fill(conn, key, m, "split-n", "BUY", 400_000, 10_000_000, T0)
    _redeem(conn, key, m, 20_000_001, T0 + 2 * H)
    conn.close()
    bets = _by_outcome(_bets(address))
    assert [(b.status, b.exit, b.back, b.closedAt) for b in (bets["Yes"], bets["No"])] == [
        ("split", 0.5, "15000001", T0 + 2 * H),
        ("split", 0.5, "5000000", T0 + 2 * H),
    ]


def test_duplicate_redeem_logs_pay_once():
    conn = fresh_test_conn()
    address, key = _account(conn)
    m = _market(conn, "dupe", payouts=(1, 0), resolved_at=T0 + H)
    _fill(conn, key, m, "dupe-y", "BUY", 400_000, 100_000_000, T0)
    for at in (T0 + 3 * H, T0 + 2 * H):
        _redeem(conn, key, m, 100_000_000, at)
    conn.close()
    (bet,) = _bets(address)
    assert [(f.side, f.at, f.dollars) for f in bet.fills] == [("REDEEM", T0 + 2 * H, "100000000"), ("BUY", T0, "40000000")]
    assert bet.back == "100000000"


def test_the_bets_add_up_to_what_the_account_earned():
    conn = fresh_test_conn()
    address, key = _account(conn)
    deposited = 100_000_000_000
    fills = [
        ("open", None, "open-y", "BUY", 123_457, 10_500_001),
        ("won", (1, 0), "won-y", "BUY", 350_000, 100_000_000),
        ("lost", (1, 0), "lost-n", "BUY", 333_333, 7_000_003),
        ("sold", None, "sold-y", "BUY", 480_000, 20_000_000),
        ("sold", None, "sold-y", "SELL", 510_000, 20_000_000),
        ("partial", (1, 0), "partial-y", "BUY", 400_000, 100_000_000),
        ("partial", (1, 0), "partial-y", "SELL", 700_000, 40_000_000),
        ("unpaid", (0, 1), "unpaid-n", "BUY", 777_777, 9_999_999),
    ]
    seeds = {seed: payouts for seed, payouts, *_ in fills}
    markets = {seed: _market(conn, seed, payouts=payouts, resolved_at=T0 + H) for seed, payouts in seeds.items()}
    for k, (seed, _, token, side, price, size) in enumerate(fills):
        _fill(conn, key, markets[seed], token, side, price, size, T0 + k)
    redeems = [("won", 100_000_000), ("partial", 60_000_000)]
    for seed, amount in redeems:
        _redeem(conn, key, markets[seed], amount, T0 + 2 * H)
    conn.close()
    positions = [
        PositionWire(asset="open-y", size=10.500001, curPrice=0.2, currentValue=2.1000002, sellableValue=2.0),
        PositionWire(asset="unpaid-n", size=9.999999, curPrice=1.0, currentValue=9.999999, settled=True),
    ]
    bets = _bets(address, positions)
    cash = deposited + sum(
        (price * size // 1_000_000) * (-1 if side == "BUY" else 1) for *_, side, price, size in fills
    ) + sum(amount for _, amount in redeems)
    value = round(sum(p.currentValue for p in positions) * 1_000_000)
    assert len(bets) == 6
    assert abs(sum(int(b.pnl) for b in bets) - (cash + value - deposited)) <= 2 * len(bets)


def test_open_bets_come_newest_first_then_closed_bets_by_close():
    conn = fresh_test_conn()
    address, key = _account(conn)
    for seed, opened, sold in (("o1", T0, None), ("o2", T0 + H, None), ("c1", T0 - 5 * H, T0 + 2 * H), ("c2", T0 - 4 * H, T0 + H)):
        m = _market(conn, seed)
        _fill(conn, key, m, f"{seed}-y", "BUY", 500_000, 10_000_000, opened)
        if sold is not None:
            _fill(conn, key, m, f"{seed}-y", "SELL", 500_000, 10_000_000, sold)
    conn.close()
    assert [b.id for b in _bets(address)] == ["o2-y", "o1-y", "c1-y", "c2-y"]


def test_prices_are_each_hour_s_last_print_from_the_one_before_the_window():
    conn = fresh_test_conn()
    address, key = _account(conn)
    short = _market(conn, "short")
    long = _market(conn, "long")
    _fill(conn, key, short, "short-y", "BUY", 300_000, 10_000_000, T0)
    _fill(conn, key, long, "long-y", "BUY", 300_000, 10_000_000, T0 - 30 * 24 * H)
    _fill(conn, key, long, "long-y", "SELL", 300_000, 10_000_000, T0 - 24 * H)
    for price, at in ((300_000, T0 - 9_000), (310_000, T0 - 7_000), (320_000, T0 - 6_000)):
        _print(conn, "short-y", price, at)
    _print(conn, "short-y", 600_000, T0 + 100, other="short-n")
    lead = 29 * 24 * H // 10
    for price, at in ((100_000, T0 - 30 * 24 * H - lead - 10), (200_000, T0 - 30 * 24 * H - lead + 10), (900_000, T0 - 24 * H + 10)):
        _print(conn, "long-y", price, at)
    conn.close()
    bets = {b.id: b for b in _bets(address)}
    start = T0 - 7_200
    assert bets["short-y"].prices == PriceLine(start=start, minutes=[0, 20, 122], permille=[300, 320, 400])
    assert bets["long-y"].prices == PriceLine(start=T0 - 30 * 24 * H - lead, minutes=[0, 0], permille=[100, 200])


def test_prices_keep_at_most_80_points_and_are_null_without_a_print():
    conn = fresh_test_conn()
    address, key = _account(conn)
    busy, quiet = _market(conn, "busy"), _market(conn, "quiet")
    _fill(conn, key, busy, "busy-y", "BUY", 500_000, 10_000_000, NOW - 200 * H)
    _fill(conn, key, quiet, "quiet-y", "BUY", 500_000, 10_000_000, NOW - H)
    for k in range(200):
        _print(conn, "busy-y", 1_000 * k, NOW - 200 * H + k * H)
    conn.close()
    bets = {b.id: b for b in _bets(address)}
    line = bets["busy-y"].prices
    assert line is not None and len(line.minutes) == len(line.permille) == 68
    assert (line.minutes[0], line.minutes[1], line.minutes[-1]) == (0, 180, 199 * 60)
    assert (line.permille[1], line.permille[-1]) == (3, 199)
    assert bets["quiet-y"].prices is None


def test_a_closed_bet_keeps_its_prices_and_an_open_bet_is_read_again_once_the_hour_turns(monkeypatch):
    conn = fresh_test_conn()
    address, key = _account(conn)
    live, done = _market(conn, "live"), _market(conn, "done")
    _fill(conn, key, live, "live-y", "BUY", 500_000, 10_000_000, T0)
    _fill(conn, key, done, "done-y", "BUY", 500_000, 10_000_000, T0)
    _fill(conn, key, done, "done-y", "SELL", 600_000, 10_000_000, T0 + H)
    for token in ("live-y", "done-y"):
        _print(conn, token, 550_000, T0 + 60)
    conn.close()
    read: list[list[str]] = []
    real = TableRead.hourly_prices

    def recorded(db, windows):
        read.append([t for t, _, _ in windows])
        return real(db, windows)

    monkeypatch.setattr(TableRead, "hourly_prices", staticmethod(recorded))
    first = _bets(address)
    second = _bets(address, previous=first)
    third = _bets(address, previous=second, previous_at=NOW)
    assert read == [["live-y", "done-y"], ["live-y"], []]
    assert second == first == third


def test_the_account_s_fills_are_read_once(monkeypatch):
    conn = fresh_test_conn()
    address, key = _account(conn)
    for seed in ("a", "b", "c"):
        m = _market(conn, seed, payouts=(1, 0), resolved_at=T0)
        _fill(conn, key, m, f"{seed}-y", "BUY", 500_000, 10_000_000, T0 - H)
    conn.close()
    statements: list[str] = []
    real = psycopg.Connection.execute

    def counted(self, query, params=None, **kwargs):
        statements.append(str(query))
        return real(self, query, params, **kwargs)

    monkeypatch.setattr(psycopg.Connection, "execute", counted)
    assert len(_bets(address)) == 3
    assert sum("FROM trades WHERE STATUS != 'FAILED'" in s for s in statements) == 1
