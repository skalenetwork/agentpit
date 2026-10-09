from fastapi.testclient import TestClient

from agentpit.api.main import app
from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.market_state import MarketState
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.liquidity import feed
from agentpit.services.account_service import AccountService
from tests.db_helpers import fresh_test_conn


def _market(conn, slug: str, volume: float) -> tuple[str, str]:
    event = TableWrite.upsert_event(conn, slug=slug, title=slug, category="Politics")
    TableWrite.update_event_volume(conn, event.event_id, volume)
    market = TableWrite.create_market(
        conn,
        CreateMarketRequest(
            question=f"{slug}?",
            description="d",
            erc1155_tokens=[(f"{slug}-y", "Yes"), (f"{slug}-n", "No")],
            slug=slug,
            condition_id=ConditionId("0x" + slug.encode().hex().ljust(64, "0")),
            state=MarketState.ACTIVE,
            event_id=event.event_id,
        ),
        is_polygon_market=False,
    )
    return market.erc1155_tokens[0][0], market.erc1155_tokens[1][0]


def test_book_price_and_midpoint_read_polymarkets_book_for_yes_and_no(house_book):
    with fresh_test_conn() as conn:
        yes, no = _market(conn, "rain", 1)
    house_book(yes, bids=(("0.4", "10"), ("0.39", "20")), asks=(("0.6", "5"),))
    tops = feed.tops([yes, no, "unknown"])

    with TestClient(app) as client:
        books = {
            t: client.get("/book", params={"token_id": t}).json() for t in (yes, no)
        }
        prices = {
            (t, side): client.get("/price", params={"token_id": t, "side": side}).json()
            for t in (yes, no)
            for side in ("BUY", "SELL")
        }
        mids = {
            t: client.get("/midpoint", params={"token_id": t}).json() for t in (yes, no)
        }

    assert [(b["bids"], b["asks"]) for b in books.values()] == [
        (
            [{"price": "0.4", "size": "10"}, {"price": "0.39", "size": "20"}],
            [{"price": "0.6", "size": "5"}],
        ),
        (
            [{"price": "0.4", "size": "5"}],
            [{"price": "0.6", "size": "10"}, {"price": "0.61", "size": "20"}],
        ),
    ]
    assert {k: v["price"] for k, v in prices.items()} == {
        (yes, "BUY"): "0.6",
        (yes, "SELL"): "0.4",
        (no, "BUY"): "0.6",
        (no, "SELL"): "0.4",
    }
    assert {t: m["mid"] for t, m in mids.items()} == {yes: "0.5", no: "0.5"}
    assert tops == {yes: (400_000, 600_000), no: (400_000, 600_000)}


def test_without_a_live_book_every_read_fails_closed(house_book):
    with fresh_test_conn() as conn:
        yes, _ = _market(conn, "rain", 1)
    house_book(yes, asks=(("0.6", "5"),)).mark_stale()
    tops, sided = feed.tops([yes]), feed.sided()

    with TestClient(app) as client:
        book = client.get("/book", params={"token_id": yes}).json()
        price = client.get("/price", params={"token_id": yes, "side": "BUY"})

    assert (book["bids"], book["asks"], price.status_code) == ([], [], 404)
    assert (tops, sided) == ({}, [])


def test_search_limits_after_the_two_sided_filter(house_book):
    with fresh_test_conn() as conn:
        busy, _ = _market(conn, "busy", 90)
        quiet, _ = _market(conn, "quiet", 10)
        _market(conn, "quieter", 5)
        house_book(busy, bids=(("0.4", "1"),))
        house_book(quiet, bids=(("0.4", "1"),), asks=(("0.6", "1"),))
        found = TableRead.search_live_markets(
            conn, query=None, limit=1, sided=feed.sided()
        )
    assert [m.slug for m in found] == ["quiet"]


def test_valuation_marks_at_the_mid_and_sells_into_the_bids(house_book):
    with fresh_test_conn() as conn:
        yes, no = _market(conn, "rain", 1)
        house_book(yes, bids=(("0.3", "10"),), asks=(("0.4", "10"), ("0.41", "10")))
        priced = {
            t: AccountService._live_pricing(conn, t, o, 15_000_000)
            for t, o in ((yes, no), (no, yes))
        }
    assert {
        t: (p.cur_price, p.sellable.size, p.sellable.value) for t, p in priced.items()
    } == {
        yes: (0.35, 10.0, 3.0),
        no: (0.65, 15.0, 0.6 * 10 + 0.59 * 5),
    }


def test_a_bookless_no_is_valued_at_one_minus_the_last_yes_print():
    with fresh_test_conn() as conn:
        yes, no = _market(conn, "dry", 1)
        conn.execute(
            "INSERT INTO trades (TRADE_ID, ASSET_ID, MAKER_ASSET_ID, MATCH_KIND, "
            "SIDE, PRICE, TRADE_SIZE, STATUS, MATCH_TIME, TAKER_API_KEY, "
            "MAKER_API_KEY) VALUES ('t', %s, %s, 'NORMAL', 'BUY', 300000, 100, "
            "'MIRRORED', 1000, 'tk', 'mk')",
            (yes, yes),
        )
        assert AccountService._live_pricing(conn, no, yes, 0).cur_price == 0.7
