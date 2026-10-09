import secrets
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from agentpit.config import Settings
from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.market_state import MarketState
from agentpit.datastructures.orderbook_summary import OrderBookLevel, OrderBookSummary
from agentpit.datastructures.user import User
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import InsufficientBalanceError
from agentpit.domain.text import clean
from agentpit.liquidity.replica import BookReplica
from agentpit.onchain.admin import OnchainAdmin
from agentpit.onchain.contracts import Contracts
from agentpit.onchain.deployment import Deployment
from agentpit.onchain.web3_client import Web3Client
from agentpit.services.agent_desk import AgentDesk, shares_for_usd, snap
from agentpit.services.leaderboard_service import RANK_FLOOR, drain, pct
from tests.db_helpers import fresh_test_db
from tests.onchain._helpers import create_market, fresh_client, house, register


def _desk(settings: Settings | None = None) -> AgentDesk:
    settings = settings or Settings()
    deployment = Deployment.load(settings.deployment_path)
    client = Web3Client(settings, deployment)
    return AgentDesk(fresh_test_db(), OnchainAdmin(client, Contracts(client.web3, deployment)), settings)


def _user(api_key: str) -> User:
    with fresh_test_db().read() as conn:
        user = TableRead.get_user_by_api_key(conn, api_key)
    assert user is not None
    return user


def _asks(house_book) -> tuple[AgentDesk, User, str, BookReplica]:
    desk = _desk()
    client = fresh_client()
    agent = _user(register(client)["api_key"])
    market = create_market(client)
    book = house_book(
        market["erc1155_tokens"][0][0],
        asks=(("0.4", "50"), ("0.45", "100")),
        user=house(client),
    )
    return desk, agent, market["slug"], book


def _drain(book: BookReplica, price: str) -> None:
    book.apply_price_change_entry(
        {"asset_id": book.asset_id, "side": "SELL", "price": price, "size": "0"}
    )


def test_snap_rounds_buys_down_and_sells_up():
    assert snap(452_500, True) == 452_000
    assert snap(452_500, False) == 453_000
    assert snap(452_000, False) == 452_000


def test_clean_flattens_and_caps():
    assert clean(" a\n\tb  c ", 10) == "a b c"
    assert clean("abcdef", 4) == "abc…"


def test_shares_for_usd_walks_levels_inside_the_limit():
    asks = [(400_000, 50_000_000), (450_000, 100_000_000)]
    assert shares_for_usd(asks, 40_000_000, 420_000, True) == (50_000_000, 20_000_000)
    assert shares_for_usd(asks, 40_000_000, 460_000, True) == (94_444_444, 39_999_999)
    bids = [(600_000, 10_000_000), (550_000, 10_000_000)]
    assert shares_for_usd(bids, 8_000_000, 540_000, False) == (13_636_363, 7_999_999)


def _seed(
    put: Callable[..., BookReplica],
    question: str,
    *,
    sides: tuple[str, ...],
    category: str,
    volume: float,
    state: MarketState,
    polymarket_id: int | None = None,
    series: str | None = None,
    kickoff: int | None = None,
) -> str:
    seed = uuid.uuid4().hex[:8]
    with fresh_test_db().write() as conn:
        event = TableWrite.upsert_event(conn, slug=f"ev-{seed}", title=question, category=category)
        TableWrite.update_event_volume(conn, event.event_id, volume)
        TableWrite.refresh_event(
            conn,
            event_id=event.event_id,
            slug=event.slug,
            title=question,
            icon_url=None,
            end_date=None,
            start_time=kickoff,
            game_id=None,
            series_slug=series,
        )
        market = TableWrite.create_market(
            conn,
            CreateMarketRequest(
                question=question,
                description="d",
                erc1155_tokens=[(f"{seed}1", "Yes"), (f"{seed}2", "No")],
                slug=seed,
                condition_id=ConditionId("0x" + secrets.token_hex(32)),
                state=state,
                event_id=event.event_id,
                polymarket_id=polymarket_id,
                end_date=kickoff,
            ),
            is_polygon_market=False,
        )
        if kickoff:
            TableWrite.replace_market_tags(conn, market_id=market.market_id, tags=[("games", "Games")])
    put(
        market.erc1155_tokens[0][0],
        bids=(("0.4", "1"),) if "BUY" in sides else (),
        asks=(("0.6", "1"),) if "SELL" in sides else (),
    )
    return seed


def test_search_lists_only_live_two_sided_markets_busiest_first(house_book):
    live = ("BUY", "SELL")
    _seed(
        house_book,
        "Will bitcoin reach 100k?",
        sides=live,
        category="Crypto",
        volume=10,
        state=MarketState.ACTIVE,
    )
    paris = _seed(
        house_book,
        "Will it rain in Paris?",
        sides=live,
        category="Weather",
        volume=50,
        state=MarketState.ACTIVE,
        polymarket_id=7,
    )
    _seed(
        house_book,
        "Will ether flip bitcoin?",
        sides=("BUY",),
        category="Crypto",
        volume=90,
        state=MarketState.ACTIVE,
    )
    _seed(
        house_book,
        "Will the Lakers win?",
        sides=live,
        category="Sports",
        volume=90,
        state=MarketState.ACTIVE,
    )
    _seed(
        house_book,
        "Will bitcoin halve?",
        sides=live,
        category="Crypto",
        volume=90,
        state=MarketState.CLOSED,
    )
    desk = _desk(Settings(excluded_categories=["Sports"]))

    listed = desk.search_markets().markets

    assert [m.question for m in listed] == ["Will it rain in Paris?", "Will bitcoin reach 100k?"]
    assert [(q.name, q.bid, q.ask) for q in listed[0].outcomes] == [
        ("Yes", 0.4, 0.6),
        ("No", 0.4, 0.6),
    ]
    assert [m.category for m in listed] == ["Weather", "Crypto"]
    assert [m.url for m in listed] == [f"https://polymarket.com/market/{paris}", None]
    assert [m.question for m in desk.search_markets("bitcoins").markets] == ["Will bitcoin reach 100k?"]


def test_a_game_gives_its_kickoff_and_is_found_by_its_league(house_book):
    live = ("BUY", "SELL")
    kickoff = int(time.time()) + 3600
    game = _seed(
        house_book,
        "Georgia vs. Alabama",
        sides=live,
        category="Sports",
        volume=90,
        state=MarketState.ACTIVE,
        series="cfb-2026",
        kickoff=kickoff,
    )
    _seed(
        house_book,
        "Will bitcoin reach 100k?",
        sides=live,
        category="Crypto",
        volume=10,
        state=MarketState.ACTIVE,
        series="btc-daily",
    )
    desk = _desk()

    first, second = desk.search_markets().markets
    detail = desk.get_market(game)

    starts = datetime.fromtimestamp(kickoff, UTC)
    assert (first.market, first.starts_at, first.closes_at, second.starts_at) == (game, starts, None, None)
    assert (detail.starts_at, detail.closes_at) == (starts, None)
    for query in ("cfb", "NCAAF", "college football", "alabama"):
        assert [m.market for m in desk.search_markets(query).markets] == [game], query
    assert desk.search_markets("nfl").markets == []


def test_trade_now_fills_at_polymarkets_levels_once_per_level(house_book):
    desk, agent, slug, book = _asks(house_book)

    by_usd = desk.trade(agent, slug, "yes", "buy", usd=30)
    used_up = desk.trade(agent, slug, "Yes", "buy", shares=30)
    _drain(book, "0.4")
    by_shares = desk.trade(agent, slug, "Yes", "buy", shares=30)

    assert by_usd.order_id is not None
    assert by_usd.profile_url == f"https://agentpit.dev/agents/{agent.eth_address}"
    assert (by_usd.status, by_usd.filled_shares, by_usd.avg_price, by_usd.usd) == ("partial", 50, 0.4, 20)
    assert used_up.status == "unfilled"
    assert (by_shares.status, by_shares.filled_shares, by_shares.avg_price, by_shares.usd) == ("filled", 30, 0.45, 13.5)
    assert by_shares.resting_shares == 0


def test_trade_with_limit_price_rests_and_cancels(house_book):
    desk, agent, slug, _ = _asks(house_book)

    first = desk.trade(agent, slug, "YES", "buy", usd=10, limit_price=0.2)
    desk.trade(agent, slug, "YES", "buy", shares=5, limit_price=0.1)

    assert (first.status, first.filled_shares, first.avg_price, first.resting_shares, first.profile_url) == (
        "resting", 0, None, 50, None
    )
    assert desk.cancel(agent, first.order_id).cancelled == 1
    assert desk.cancel(agent).cancelled == 1
    assert desk.cancel(agent).cancelled == 0


def test_trade_the_book_moved_away_from_is_unfilled(
    house_book, monkeypatch: pytest.MonkeyPatch
):
    client = fresh_client()
    agent = _user(register(client)["api_key"])
    market = create_market(client)
    slug, token = market["slug"], market["erc1155_tokens"][0][0]
    house_book(token, asks=(("0.5", "10"),))
    desk = _desk()
    stale = OrderBookSummary(
        market="m", asset_id=token, timestamp="0", hash="h", asks=[OrderBookLevel(price="0.4", size="10")]
    )
    monkeypatch.setattr(desk._orders, "get_book", lambda _token: stale)

    result = desk.trade(agent, slug, "YES", "buy", shares=10)

    assert result.model_dump() == {
        "order_id": None,
        "status": "unfilled",
        "filled_shares": 0,
        "avg_price": None,
        "usd": 0,
        "resting_shares": 0,
        "profile_url": None,
    }


def test_insufficient_cash_states_have_and_need(house_book):
    desk, agent, slug, _ = _asks(house_book)

    with pytest.raises(InsufficientBalanceError, match=r"need \$500,000\.00, cash is \$100,000\.00"):
        desk.trade(agent, slug, "YES", "buy", shares=1_000_000, limit_price=0.5)


def test_portfolio_matches_the_leaderboard(house_book):
    desk, agent, slug, book = _asks(house_book)
    desk.trade(agent, slug, "YES", "buy", usd=30)
    _drain(book, "0.4")
    desk.trade(agent, slug, "YES", "buy", shares=10, limit_price=0.1)
    desk._board.take_snapshot(int(time.time()), drain())
    page = f"https://agentpit.dev/agents/{agent.eth_address}"
    dated = f"{page}?d={datetime.now(UTC).date()}"

    warming = desk.portfolio(agent)

    assert warming.address == agent.eth_address
    assert warming.cash_usd == 100_000 - 20
    assert warming.equity_usd == pytest.approx(warming.cash_usd + warming.positions_value_usd)
    assert (warming.trades, warming.rank, warming.rank_change, warming.trades_to_rank, warming.ranked_agents) == (
        1, None, None, RANK_FLOOR - 1, 0
    )
    assert warming.profile_url == page
    assert warming.share == f"{warming.agent} is warming up on AgentPit, 1 of {RANK_FLOOR} trades to rank: {dated}"
    assert [(h.market, h.url, h.outcome, h.shares, h.avg_price) for h in warming.positions] == [(slug, None, "YES", 50, 0.4)]
    assert [(o.market, o.side, o.price, o.shares) for o in warming.open_orders] == [(slug, "buy", 0.1, 10)]
    assert warming.next_top_up_at is None
    assert desk.leaderboard().agents == []

    for _ in range(RANK_FLOOR - 1):
        desk.trade(agent, slug, "YES", "buy", shares=1)
    desk._board.take_snapshot(int(time.time()), drain())

    mine = desk.portfolio(agent)
    board = desk.leaderboard()
    standing = next(s for s in board.agents if s.agent == mine.agent)

    assert (mine.equity_usd, mine.pnl_usd, mine.rank) == (standing.equity_usd, standing.pnl_usd, standing.rank)
    assert (mine.trades, mine.trades_to_rank, mine.rank_change) == (RANK_FLOOR, 0, None)
    assert mine.ranked_agents == board.total == 1
    assert mine.share == (
        f"{mine.agent} is #{mine.rank} of 1 on AgentPit with a {pct(standing.return_pct)} return on paper money: {dated}"
    )


def test_top_up_during_cooldown_adds_nothing():
    desk = _desk(Settings.model_validate({"AGENTPIT_PAPER_BALANCE_TARGET_RAW": 10**15}))
    agent = _user(register(fresh_client())["api_key"])
    with fresh_test_db().write() as conn:
        TableWrite.set_last_topup_at(conn, agent.user_id, int(time.time()))

    result = desk.top_up(agent)

    assert result.added_usd == 0
    assert result.next_top_up_at is not None


def test_leaderboard_carries_the_agent_app():
    with fresh_test_db().write() as conn:
        user_id, acct, api_key = TableWrite.create_user(
            conn, email=None, password_hash=None, handle="claude_bot", owner_workos_id="user_o", agent_app="Claude"
        )
        for k in range(RANK_FLOOR):
            conn.execute(
                "INSERT INTO trades (TRADE_ID, TAKER_API_KEY, MATCH_TIME, STATUS) VALUES (%s, %s, 0, 'CONFIRMED')",
                (f"t{k}", api_key),
            )
        TableWrite.insert_account_snapshot(conn, user_id, 1, 110_000_000_000, 100_000_000_000)

    board = _desk().leaderboard()

    assert [(s.rank, s.rank_change, s.agent, s.app, s.return_pct, s.pnl_usd, s.url) for s in board.agents] == [
        (1, None, "claude_bot", "Claude", 10, 10_000, f"https://agentpit.dev/agents/{acct.address}")
    ]


def test_the_desk_reads_positions_with_the_configured_claim_minimum():
    """The agent tools must show the same Claim state as the web profile."""
    desk = _desk(Settings(min_claim_micro=123_456))
    assert desk._accounts._min_claim_micro == 123_456  # noqa: SLF001
