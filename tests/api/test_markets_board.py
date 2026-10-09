import hashlib
import time
import uuid
from collections.abc import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

from agentpit.api.deps import get_db_session, get_onchain_admin
from agentpit.api.main import app
from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.market import Market
from agentpit.datastructures.market_state import MarketState
from agentpit.datastructures.position_wire import PositionWire
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.liquidity.replica import BookReplica
from agentpit.polymarket.format import price_to_decimal_str
from agentpit.polymarket.polymarket_sync import attach_teams
from agentpit.services import board_service
from agentpit.services.leaderboard_service import Holdings, _holdings
from tests.db_helpers import fresh_test_conn

NOW = int(time.time())
DAY = 86_400
G = 100_000_000_000


class _NoChain:
    def usd_balance(self, address: str) -> int:
        raise AssertionError("the board must not read the chain")


@pytest.fixture(autouse=True)
def _board():
    board_service._snapshot = None
    chain = app.dependency_overrides[get_onchain_admin]
    app.dependency_overrides[get_onchain_admin] = lambda: _NoChain()
    yield
    app.dependency_overrides[get_onchain_admin] = chain


def _event(
    conn,
    slug: str,
    title: str,
    category: str,
    *,
    volume: float | None = None,
    end: int | None = None,
    start: int | None = None,
    series: str | None = None,
    polymarket: bool = True,
) -> int:
    event = TableWrite.upsert_event(
        conn,
        slug=slug,
        title=title,
        category=category,
        end_date=end,
        polymarket_event_id=f"pe-{slug}" if polymarket else None,
    )
    TableWrite.update_event_volume(conn, event.event_id, volume)
    TableWrite.refresh_event(
        conn,
        event_id=event.event_id,
        slug=slug,
        title=title,
        icon_url=None,
        end_date=None,
        start_time=start,
        game_id=None,
        series_slug=series,
    )
    return event.event_id


def _market(
    put: Callable[..., BookReplica],
    conn,
    event_id: int,
    slug: str,
    question: str,
    *,
    sides: tuple[str, str] = ("Yes", "No"),
    label: str | None = None,
    book: tuple[int, int] | None = None,
    change: float | None = None,
    tags: tuple[str, ...] = (),
    polymarket: bool = True,
) -> Market:
    market = TableWrite.create_market(
        conn,
        CreateMarketRequest(
            question=question,
            description="d",
            erc1155_tokens=[(f"{slug}-{k}", side) for k, side in enumerate(sides)],
            slug=slug,
            condition_id=ConditionId("0x" + hashlib.sha256(slug.encode()).hexdigest()),
            state=MarketState.ACTIVE,
            event_id=event_id,
            outcome_label=label,
            polymarket_id=(
                int(hashlib.sha256(slug.encode()).hexdigest()[:8], 16)
                if polymarket
                else None
            ),
        ),
        is_polygon_market=False,
    )
    if change is not None:
        conn.execute(
            "UPDATE markets SET PRICE_CHANGE_24H = %s WHERE MARKET_ID = %s",
            (change, market.market_id),
        )
    TableWrite.replace_market_tags(
        conn, market_id=market.market_id, tags=[(t, t) for t in tags]
    )
    if book is not None:
        bid, ask = (((price_to_decimal_str(p), "1"),) for p in book)
        put(market.erc1155_tokens[0][0], bids=bid, asks=ask)
    return market


def _agent(conn, handle: str) -> str:
    user_id, acct, key = TableWrite.create_user(
        conn, email=f"{handle}@example.com", password_hash="x", handle=handle
    )
    conn.execute(
        "INSERT INTO trades (TRADE_ID, MARKET, ASSET_ID, SIDE, PRICE, TRADE_SIZE, MATCH_KIND, STATUS, TAKER_API_KEY, MATCH_TIME) "
        "VALUES (%s, '0x00', 'x', 'BUY', 250000, 4000000, 'NORMAL', 'CONFIRMED', %s, %s)",
        (uuid.uuid4().hex, key, NOW - 60),
    )
    TableWrite.insert_account_snapshot(conn, user_id, NOW - 30, G, G, 0, 0)
    return acct.address


def _bet(
    market: Market, side: int, value: float, pnl: float, *, settled: bool = False
) -> PositionWire:
    return PositionWire(
        conditionId=market.condition_id.value,
        outcome=market.erc1155_tokens[side][1],
        outcomeIndex=side,
        avgPrice=0.5,
        currentValue=value,
        cashPnl=pnl,
        settled=settled,
    )


def _world(put: Callable[..., BookReplica]) -> dict[str, str]:
    conn = fresh_test_conn()
    fed = _event(
        conn,
        "fed",
        "Fed decision in October?",
        "Politics",
        volume=1000,
        end=NOW + 2 * DAY,
    )
    cut = _market(
        put,
        conn,
        fed,
        "cut",
        "Will the Fed cut?",
        label="Cut",
        book=(590_000, 610_000),
        change=0.04,
    )
    _market(
        put,
        conn,
        fed,
        "hold",
        "Will the Fed hold?",
        label="Hold",
        book=(290_000, 310_000),
        change=-0.08,
    )
    hike = _market(
        put,
        conn,
        fed,
        "hike",
        "Will the Fed hike?",
        label="Hike",
        book=(40_000, 60_000),
    )
    _market(
        put,
        conn,
        fed,
        "big",
        "Will the Fed cut 50?",
        label="Big cut",
        book=(30_000, 50_000),
    )
    _market(
        put,
        conn,
        _event(conn, "btc", "Bitcoin above 100k?", "Crypto", volume=500),
        "btc",
        "Bitcoin above 100k?",
        book=(690_000, 710_000),
        change=0.005,
    )
    game = _event(
        conn,
        "nfl-atl-no",
        "Falcons vs. Saints",
        "Sports",
        volume=300,
        start=NOW + 3600,
        series="nfl-2026",
    )
    _market(
        put,
        conn,
        game,
        "atl-no",
        "Falcons vs. Saints",
        sides=("Falcons", "Saints"),
        book=(450_000, 470_000),
        change=0.03,
        tags=("games", "nfl"),
    )
    cs2 = _event(
        conn,
        "cs2-m80",
        "Counter-Strike: M80 vs TYLOO (BO3) - ESL Pro League",
        "Sports",
        volume=200,
        start=NOW + 7200,
        series="counter-strike",
    )
    _market(
        put,
        conn,
        cs2,
        "m80",
        "M80 vs TYLOO",
        sides=("M80", "TYLOO"),
        book=(500_000, 520_000),
        tags=("games", "esports", "counter-strike-2"),
    )
    lol = _event(
        conn,
        "lol-t1",
        "LoL: T1 vs Gen.G (BO5) - Worlds",
        "Sports",
        volume=150,
        start=NOW + 10_800,
        series="league-of-legends",
    )
    _market(
        put,
        conn,
        lol,
        "t1",
        "T1 vs Gen.G",
        sides=("T1", "Gen.G"),
        book=(500_000, 520_000),
        tags=("games", "esports", "league-of-legends"),
    )
    bowl = _event(
        conn,
        "super-bowl",
        "Super Bowl Champion 2027",
        "Sports",
        volume=100,
        end=NOW + 100 * DAY,
    )
    chiefs = _market(
        put,
        conn,
        bowl,
        "chiefs",
        "Will the Chiefs win Super Bowl 2027?",
        book=(200_000, 220_000),
        tags=("nfl",),
    )
    soccer = _event(
        conn,
        "unl-cyp-lat",
        "Cyprus vs. Latvia",
        "Sports",
        volume=50,
        start=NOW + 600,
        series="soccer-unl",
    )
    for slug, label in (
        ("lat", "Latvia"),
        ("draw", "Draw (Cyprus vs. Latvia)"),
        ("cyp", "Cyprus"),
    ):
        _market(
            put, conn, soccer, slug, f"{label}?", label=label, tags=("games", "soccer")
        )
    _market(
        put,
        conn,
        _event(conn, "stale", "Will the stale thing happen?", "Politics", volume=10),
        "stale",
        "Will the stale thing happen?",
        book=(400_000, 420_000),
    )
    local = _market(
        put,
        conn,
        _event(
            conn, "local", "Will the local thing happen?", "Politics", polymarket=False
        ),
        "local",
        "Will the local thing happen?",
        polymarket=False,
    )
    _market(
        put,
        conn,
        _event(conn, "dead", "Will the dead thing happen?", "Politics"),
        "dead",
        "Will the dead thing happen?",
    )
    shutdown = _market(
        put,
        conn,
        _event(conn, "shutdown", "Will the shutdown end?", "Politics"),
        "shutdown",
        "Will the shutdown end?",
    )
    TableWrite.set_market_state(
        conn, shutdown.market_id, MarketState.ACTIVE, MarketState.RESOLVED, (1, 0)
    )
    sunny = _market(
        put,
        conn,
        _event(conn, "sunny", "Was it sunny?", "Science"),
        "sunny",
        "Was it sunny?",
    )
    TableWrite.set_market_state(
        conn, sunny.market_id, MarketState.ACTIVE, MarketState.RESOLVED, (0, 1)
    )
    for k in range(45):
        _market(
            put,
            conn,
            _event(conn, f"filler-{k}", f"Filler question {k}?", "Politics"),
            f"filler-{k}",
            f"Filler question {k}?",
            book=(100_000, 120_000),
        )
    ada, bob = _agent(conn, "Ada"), _agent(conn, "Bob")
    conn.close()
    _holdings[ada] = Holdings(
        NOW, 0, [_bet(hike, 0, 20.0, 15.0), _bet(chiefs, 1, 20.0, -2.0)], []
    )
    _holdings[bob] = Holdings(
        NOW,
        0,
        [
            _bet(cut, 1, 10.0, 1.0),
            _bet(local, 0, 50.0, 5.0),
            _bet(shutdown, 0, 100.0, 60.0, settled=True),
        ],
        [],
    )
    return {"ada": ada, "bob": bob}


def _get(client: TestClient, **params) -> dict:
    resp = client.get("/markets/board", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_tabs_count_live_events_and_trending_follows_volume(house_book):
    agents = _world(house_book)
    with TestClient(app) as client:
        resp = client.get("/markets/board")
        body = resp.json()
    assert resp.headers["cache-control"] == "public, max-age=30"
    assert [
        (t["key"], t["label"], t["count"], t["category"]) for t in body["tabs"]
    ] == [
        ("trending", "Trending", 53, False),
        ("movers", "Movers", 2, False),
        ("agents", "Agents", 3, False),
        ("ending", "Ending soon", 4, False),
        ("settled", "Settled", 1, False),
        ("politics", "Politics", 48, True),
    ]
    assert (
        body["tab"],
        body["page"],
        body["pages"],
        body["total"],
        body["liveMarkets"],
    ) == ("trending", 1, 2, 53, 55)
    assert [c["slug"] for c in body["cards"][:6]] == [
        "fed",
        "btc",
        "nfl-atl-no",
        "cs2-m80",
        "lol-t1",
        "super-bowl",
    ]
    assert len(body["cards"]) == 48
    fed = body["cards"][0]
    assert (
        fed["kind"],
        fed["state"],
        fed["startTime"],
        fed["outcomeCount"],
        fed["lead"],
        fed["url"],
    ) == ("multi", "live", None, 4, 0, "https://polymarket.com/event/fed")
    assert [
        (o["label"], o["price"], o["change24h"], o["url"]) for o in fed["outcomes"]
    ] == [
        ("Cut", 0.6, 0.04, "https://polymarket.com/market/cut"),
        ("Hike", 0.05, None, "https://polymarket.com/market/hike"),
    ]
    assert fed["outcomes"][0]["bets"] == [
        {
            "agent": agents["bob"],
            "name": "Bob",
            "side": "No",
            "value": "10000000",
            "avgPrice": 0.5,
            "pnl": "1000000",
        }
    ]
    assert [b["name"] for b in fed["outcomes"][1]["bets"]] == ["Ada"]
    game = body["cards"][2]
    assert (
        game["kind"],
        [(o["label"], o["price"], o["change24h"]) for o in game["outcomes"]],
    ) == (
        "matchup",
        [("Falcons", 0.46, 0.03), ("Saints", 0.54, -0.03)],
    )
    assert game["lead"] == 1
    assert "unl-cyp-lat" not in {c["slug"] for c in body["cards"]}


def test_a_local_market_has_no_polymarket_url_and_a_stale_change_is_hidden(house_book):
    _world(house_book)
    with TestClient(app) as client:
        by_slug = {c["slug"]: c for c in _get(client, tab="politics")["cards"]}
    local = by_slug["local"]
    assert (
        local["url"],
        local["outcomes"][0]["url"],
        local["outcomes"][0]["price"],
    ) == (None, None, 0.5)
    assert by_slug["stale"]["outcomes"][0]["change24h"] is None
    assert "dead" not in by_slug


def test_movers_lead_with_the_biggest_move(house_book):
    _world(house_book)
    with TestClient(app) as client:
        body = _get(client, tab="movers")
    assert [c["slug"] for c in body["cards"]] == ["fed", "nfl-atl-no"]
    fed = body["cards"][0]
    assert [o["label"] for o in fed["outcomes"]] == ["Cut", "Hold", "Hike"]
    assert (fed["lead"], fed["outcomes"][fed["lead"]]["change24h"]) == (1, -0.08)


def test_agents_ending_and_settled_tabs(house_book):
    agents = _world(house_book)
    with TestClient(app) as client:
        held = _get(client, tab="agents")
        ending = _get(client, tab="ending")
        settled = _get(client, tab="settled")
    assert [c["slug"] for c in held["cards"]] == ["fed", "local", "super-bowl"]
    assert [c["slug"] for c in ending["cards"]] == [
        "nfl-atl-no",
        "cs2-m80",
        "lol-t1",
        "fed",
    ]
    [card] = settled["cards"]
    assert (card["slug"], card["state"], card["resolvedAt"] >= NOW, card["lead"]) == (
        "shutdown",
        "settled",
        True,
        0,
    )
    assert (
        card["outcomes"][0]["price"] == 1.0 and card["outcomes"][0]["change24h"] is None
    )
    assert card["outcomes"][0]["bets"] == [
        {
            "agent": agents["bob"],
            "name": "Bob",
            "side": "Yes",
            "value": "100000000",
            "avgPrice": 0.5,
            "pnl": "60000000",
        }
    ]


def test_a_settled_50_50_bet_stays_on_the_board(house_book):
    conn = fresh_test_conn()
    void = _market(
        house_book,
        conn,
        _event(conn, "void", "Was the match played?", "Politics"),
        "void",
        "Was the match played?",
    )
    TableWrite.set_market_state(
        conn, void.market_id, MarketState.ACTIVE, MarketState.RESOLVED, (1, 1)
    )
    cara = _agent(conn, "Cara")
    conn.close()
    redeemed = PositionWire(
        conditionId=void.condition_id.value,
        outcome="No",
        outcomeIndex=1,
        avgPrice=0.5,
        currentValue=50.0,
        curPrice=0.5,
    )
    _holdings[cara] = Holdings(NOW, 0, [], [redeemed])
    with TestClient(app) as client:
        [card] = _get(client, tab="settled")["cards"]
    assert (card["slug"], card["state"], card["outcomes"][0]["price"]) == (
        "void",
        "settled",
        0.5,
    )
    assert [(b["name"], b["side"]) for b in card["outcomes"][0]["bets"]] == [
        ("Cara", "No")
    ]


def test_search_and_paging(house_book):
    _world(house_book)
    with TestClient(app) as client:
        hold = _get(client, q=" HOLD ")
        falcons = _get(client, tab="ending", q="falcons")
        none = _get(client, q="xyz")
        last = _get(client, page=9)
        missing = client.get("/markets/board", params={"tab": "nope"})
        zero = client.get("/markets/board", params={"page": 0})
    assert (hold["q"], [c["slug"] for c in hold["cards"]]) == ("HOLD", ["fed"])
    assert [c["slug"] for c in falcons["cards"]] == ["nfl-atl-no"]
    assert (none["total"], none["pages"], none["cards"]) == (0, 1, [])
    assert (last["page"], len(last["cards"])) == (2, 5)
    assert (missing.status_code, zero.status_code) == (404, 422)


def test_search_finds_a_league_by_key_name_or_alias_and_games_carry_its_time_zone(
    house_book,
):
    _world(house_book)
    conn = fresh_test_conn()
    cfb = _event(
        conn,
        "cfb-uga-ala",
        "Georgia vs. Alabama",
        "Sports",
        start=NOW + 7200,
        series="cfb-2026",
    )
    _market(
        house_book,
        conn,
        cfb,
        "uga-ala",
        "Georgia vs. Alabama",
        sides=("Georgia", "Alabama"),
        book=(450_000, 470_000),
        tags=("games", "cfb"),
    )
    conn.close()
    with TestClient(app) as client:
        found = {
            q: _get(client, tab="sports", q=q)["sports"]
            for q in ("cfb", "NCAAF", "College football", "nfl", "football")
        }
        trending = _get(client, q="nfl")
        games = {
            g["slug"]: g["tz"] for g in _get(client, tab="sports")["sports"]["games"]
        }
    assert {q: [g["slug"] for g in s["games"]] for q, s in found.items()} == {
        "cfb": ["cfb-uga-ala"],
        "NCAAF": ["cfb-uga-ala"],
        "College football": ["cfb-uga-ala"],
        "nfl": ["nfl-atl-no"],
        "football": ["nfl-atl-no", "cfb-uga-ala"],
    }
    assert [f["slug"] for f in found["nfl"]["futures"]] == ["super-bowl"]
    assert [c["slug"] for c in trending["cards"]] == ["nfl-atl-no", "super-bowl"]
    assert (games["cfb-uga-ala"], games["nfl-atl-no"], games["cs2-m80"]) == (
        "America/New_York",
        "America/New_York",
        "UTC",
    )


def test_sports_sidebar_counts_games_futures_and_leagues(house_book):
    _world(house_book)
    with TestClient(app) as client:
        upcoming = _get(client, tab="sports")
        football = _get(client, tab="sports", sport="football")
        cs2 = _get(client, tab="sports", sport="esports/cs2")
        found = _get(client, tab="sports", q="cyprus")
        missing = client.get(
            "/markets/board", params={"tab": "sports", "sport": "football/nfl"}
        )
    sports = upcoming["sports"]
    assert (upcoming["cards"], upcoming["total"], upcoming["pages"]) == ([], 3, 1)
    assert [(v["key"], v["count"]) for v in sports["views"]] == [
        ("upcoming", 3),
        ("agents", 1),
        ("futures", 1),
        ("settled", 0),
    ]
    assert [
        (
            s["key"],
            s["label"],
            s["count"],
            [(lg["key"], lg["count"]) for lg in s["leagues"]],
        )
        for s in sports["sports"]
    ] == [
        ("esports", "Esports", 2, [("esports/cs2", 1), ("esports/lol", 1)]),
        ("football", "Football", 2, []),
    ]
    assert [g["slug"] for g in sports["games"]] == ["nfl-atl-no", "cs2-m80", "lol-t1"]
    assert (sports["noBook"], sports["settled"]) == ({"unl": 1}, 0)
    game = sports["games"][0]
    assert (
        game["league"],
        game["leagueLabel"],
        game["sport"],
        game["status"],
        game["startTime"],
    ) == ("nfl", "NFL", "football", "upcoming", NOW + 3600)
    assert (
        [g["slug"] for g in football["sports"]["games"]],
        [f["slug"] for f in football["sports"]["futures"]],
    ) == (["nfl-atl-no"], ["super-bowl"])
    assert football["sports"]["noBook"] == {}
    assert [(g["slug"], g["leagueLabel"]) for g in cs2["sports"]["games"]] == [
        ("cs2-m80", "Counter-Strike 2")
    ]
    assert (found["sports"]["games"], missing.status_code) == ([], 404)


def test_a_game_past_kickoff_is_live_while_its_book_is_two_sided_and_leads_upcoming(
    house_book,
):
    conn = fresh_test_conn()
    soccer = _event(
        conn,
        "unl-cyp-lat",
        "Cyprus vs. Latvia",
        "Sports",
        start=NOW + 600,
        series="soccer-unl",
    )
    for slug, label in (
        ("lat", "Latvia"),
        ("draw", "Draw (Cyprus vs. Latvia)"),
        ("cyp", "Cyprus"),
    ):
        _market(
            house_book,
            conn,
            soccer,
            slug,
            f"{label}?",
            label=label,
            book=(300_000, 320_000),
            tags=("games", "soccer"),
        )
    for slug, start, book in (
        ("nfl-atl-no", NOW + 3600, (450_000, 470_000)),
        ("nfl-sf-sea", NOW - 600, None),
        ("nfl-kc-buf", NOW - 1800, (450_000, 470_000)),
    ):
        _market(
            house_book,
            conn,
            _event(conn, slug, slug, "Sports", start=start, series="nfl-2026"),
            slug,
            slug,
            sides=("Away", "Home"),
            book=book,
            tags=("games", "nfl"),
        )
    conn.close()
    with TestClient(app) as client:
        sports = _get(client, tab="sports")["sports"]
    assert [(g["slug"], g["status"], g["trading"]) for g in sports["games"]] == [
        ("nfl-kc-buf", "live", True),
        ("unl-cyp-lat", "upcoming", True),
        ("nfl-atl-no", "upcoming", True),
    ]
    assert [o["label"] for o in sports["games"][1]["outcomes"]] == [
        "Cyprus",
        "Draw (Cyprus vs. Latvia)",
        "Latvia",
    ]
    assert (sports["views"][0]["count"], sports["noBook"]) == (3, {"nfl": 1})


def test_a_window_carries_its_start_and_leaves_the_board_once_resolved(house_book):
    conn = fresh_test_conn()
    _market(
        house_book,
        conn,
        _event(
            conn, "btc-updown-5m-2", "Bitcoin Up or Down 2", "Crypto", start=NOW + 60
        ),
        "w2",
        "Bitcoin Up or Down 2",
        sides=("Up", "Down"),
        book=(450_000, 470_000),
        tags=("up-or-down",),
    )
    done = [
        _market(
            house_book,
            conn,
            _event(conn, f"btc-updown-5m-{k}", f"Bitcoin Up or Down {k}", "Crypto"),
            f"w{k}",
            f"Bitcoin Up or Down {k}",
            sides=("Up", "Down"),
            tags=tags,
        )
        for k, tags in ((0, ("up-or-down",)), (1, ()))
    ]
    for market in done:
        TableWrite.set_market_state(
            conn, market.market_id, MarketState.ACTIVE, MarketState.RESOLVED, (0, 1)
        )
    loaded = {
        e.slug
        for e, _ in TableRead.board_events(
            conn, settled_since=NOW - DAY, excluded_categories=None, excluded_tags=None
        )
    }
    cara = _agent(conn, "Cara")
    conn.close()
    _holdings[cara] = Holdings(
        NOW, 0, [_bet(m, 1, 20.0, 10.0, settled=True) for m in done], []
    )
    with TestClient(app) as client:
        [card] = _get(client)["cards"]
        settled = _get(client, tab="settled")["cards"]
    assert (card["slug"], card["kind"], card["startTime"]) == (
        "btc-updown-5m-2",
        "window",
        NOW + 60,
    )
    assert (loaded, settled) == ({"btc-updown-5m-2", "btc-updown-5m-1"}, [])


def test_a_game_carries_teams_buy_prices_and_volume(house_book):
    conn = fresh_test_conn()
    nfl = _event(
        conn,
        "nfl-tb-dal",
        "Buccaneers vs. Cowboys",
        "Sports",
        volume=40_700,
        start=NOW + 3600,
        series="nfl-2026",
    )
    _market(
        house_book,
        conn,
        nfl,
        "tb-dal",
        "Buccaneers vs. Cowboys",
        sides=("Buccaneers", "Cowboys"),
        book=(450_000, 470_000),
        tags=("games", "nfl"),
    )
    soccer = _event(
        conn,
        "bra-san-fla",
        "Santos vs. Flamengo",
        "Sports",
        start=NOW + 7200,
        series="soccer-bra",
    )
    for slug, label, book in (
        ("san", "Santos", (300_000, 320_000)),
        ("draw", "Draw (Santos vs. Flamengo)", (250_000, 270_000)),
        ("fla", "Flamengo", (420_000, 440_000)),
    ):
        _market(
            house_book,
            conn,
            soccer,
            slug,
            f"{label}?",
            label=label,
            book=book,
            tags=("games", "soccer"),
        )
    conn.execute("UPDATE events SET GAME_ID = SLUG, VOLUME = VOLUME_24HR")
    conn.close()
    teams = {
        "pe-nfl-tb-dal": {
            "image": "https://x/nfl.png",
            "teams": [
                {
                    "name": "Tampa Bay Buccaneers",
                    "alias": "Buccaneers",
                    "record": "0-4-0",
                    "logo": "https://x/tb.png",
                    "abbreviation": "tb",
                    "color": "#C02020",
                },
                {
                    "name": "Dallas Cowboys",
                    "alias": "Cowboys",
                    "record": "2-2-0",
                    "logo": "https://x/nfl.png",
                    "abbreviation": "dal",
                    "color": "red",
                },
            ],
        },
        "pe-bra-san-fla": {
            "image": "https://x/bra.png",
            "teams": [
                {
                    "name": "Santos FC",
                    "alias": "Santos",
                    "record": "10-8-9",
                    "logo": "https://x/san.png",
                    "abbreviation": "san1",
                    "color": "#aa7918",
                },
                {
                    "name": "Flamengo",
                    "record": "0-0",
                    "logo": "https://x/a.png",
                    "abbreviation": "fla",
                },
            ],
        },
    }
    calls = []

    def gamma(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params.get_list("id"))
        return httpx.Response(
            200, json=[{"id": i, **teams[i]} for i in request.url.params.get_list("id")]
        )

    db = app.dependency_overrides[get_db_session]()
    with httpx.Client(
        base_url="https://gamma", transport=httpx.MockTransport(gamma)
    ) as client:
        attach_teams(db, client)
        attach_teams(db, client)
    with TestClient(app) as client:
        games = {g["slug"]: g for g in _get(client, tab="sports")["sports"]["games"]}
    nfl_game, soccer_game = games["nfl-tb-dal"], games["bra-san-fla"]
    assert (len(calls), sorted(calls[0])) == (1, ["pe-bra-san-fla", "pe-nfl-tb-dal"])
    assert (
        nfl_game["volume"],
        [(o["ask"], o["team"]) for o in nfl_game["outcomes"]],
    ) == (
        40_700,
        [
            (
                0.47,
                {
                    "logo": "https://x/tb.png",
                    "record": "0-4",
                    "color": "#C02020",
                    "abbr": "TB",
                },
            ),
            (0.55, {"logo": None, "record": "2-2", "color": None, "abbr": "DAL"}),
        ],
    )
    assert [(o["label"], o["ask"], o["team"]) for o in soccer_game["outcomes"]] == [
        (
            "Santos",
            0.32,
            {
                "logo": "https://x/san.png",
                "record": "10-8-9",
                "color": "#aa7918",
                "abbr": "SAN",
            },
        ),
        ("Draw (Santos vs. Flamengo)", 0.27, None),
        (
            "Flamengo",
            0.44,
            {"logo": None, "record": None, "color": None, "abbr": "FLA"},
        ),
    ]
