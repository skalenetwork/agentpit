import secrets
import time
import uuid

import pytest
from fastapi.testclient import TestClient

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
from agentpit.onchain.admin import OnchainAdmin
from agentpit.onchain.contracts import Contracts
from agentpit.onchain.deployment import Deployment
from agentpit.onchain.web3_client import Web3Client
from agentpit.services.agent_desk import AgentDesk, shares_for_usd, snap
from tests.db_helpers import fresh_test_db
from tests.onchain._helpers import ADMIN_HDR, create_market, fresh_client, hdr, register


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


def _live_market(client: TestClient, maker: str) -> tuple[str, str]:
    market = create_market(client)
    client.post(f"/markets/{market['market_id']}/activate", headers=ADMIN_HDR).raise_for_status()
    client.post(
        f"/markets/{market['market_id']}/split_position", headers=hdr(maker), json={"amount": 200_000_000}
    ).raise_for_status()
    return market["slug"], market["erc1155_tokens"][0][0]


def _asks() -> tuple[AgentDesk, User, str]:
    desk = _desk()
    client = fresh_client()
    maker = _user(register(client)["api_key"])
    agent = _user(register(client)["api_key"])
    slug, _ = _live_market(client, maker.api_key)
    desk.trade(maker, slug, "YES", "sell", shares=50, limit_price=0.4)
    desk.trade(maker, slug, "YES", "sell", shares=100, limit_price=0.45)
    return desk, agent, slug


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


def _seed(question: str, *, sides: tuple[str, ...], category: str, volume: float, state: MarketState) -> None:
    seed = uuid.uuid4().hex[:8]
    with fresh_test_db().write() as conn:
        event = TableWrite.upsert_event(conn, slug=f"ev-{seed}", title=question, category=category)
        TableWrite.update_event_volume(conn, event.event_id, volume)
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
            ),
            is_polygon_market=False,
        )
        for side in sides:
            conn.execute(
                "INSERT INTO orders (ORDER_ID, TOKEN_ID, SIDE, PRICE, STATUS, REMAINING_AMOUNT, EXPIRATION, "
                "CREATED_AT, API_KEY) VALUES (%s, %s, %s, %s, 'live', 1000000, 0, %s, 'k')",
                (uuid.uuid4().hex, market.erc1155_tokens[0][0], side, 400_000 if side == "BUY" else 600_000, int(time.time())),
            )


def test_search_lists_only_live_two_sided_markets_busiest_first():
    live = ("BUY", "SELL")
    _seed("Will bitcoin reach 100k?", sides=live, category="Crypto", volume=10, state=MarketState.ACTIVE)
    _seed("Will it rain in Paris?", sides=live, category="Weather", volume=50, state=MarketState.ACTIVE)
    _seed("Will ether flip bitcoin?", sides=("BUY",), category="Crypto", volume=90, state=MarketState.ACTIVE)
    _seed("Will the Lakers win?", sides=live, category="Sports", volume=90, state=MarketState.ACTIVE)
    _seed("Will bitcoin halve?", sides=live, category="Crypto", volume=90, state=MarketState.CLOSED)
    desk = _desk()

    listed = desk.search_markets().markets

    assert [m.question for m in listed] == ["Will it rain in Paris?", "Will bitcoin reach 100k?"]
    assert [(q.name, q.bid, q.ask) for q in listed[0].outcomes] == [("Yes", 0.4, 0.6), ("No", None, None)]
    assert [m.question for m in desk.search_markets("bitcoins").markets] == ["Will bitcoin reach 100k?"]


def test_trade_now_reports_the_makers_price():
    desk, agent, slug = _asks()

    by_usd = desk.trade(agent, slug, "yes", "buy", usd=30)
    by_shares = desk.trade(agent, slug, "Yes", "buy", shares=30)

    assert by_usd.order_id is not None
    assert (by_usd.status, by_usd.filled_shares, by_usd.avg_price, by_usd.usd) == ("partial", 50, 0.4, 20)
    assert (by_shares.status, by_shares.filled_shares, by_shares.avg_price, by_shares.usd) == ("filled", 30, 0.45, 13.5)
    assert by_shares.resting_shares == 0


def test_trade_with_limit_price_rests_and_cancels():
    desk, agent, slug = _asks()

    first = desk.trade(agent, slug, "YES", "buy", usd=10, limit_price=0.2)
    desk.trade(agent, slug, "YES", "buy", shares=5, limit_price=0.1)

    assert (first.status, first.filled_shares, first.avg_price, first.resting_shares) == ("resting", 0, None, 50)
    assert desk.cancel(agent, first.order_id).cancelled == 1
    assert desk.cancel(agent).cancelled == 1
    assert desk.cancel(agent).cancelled == 0


def test_trade_the_book_moved_away_from_is_unfilled(monkeypatch: pytest.MonkeyPatch):
    client = fresh_client()
    agent = _user(register(client)["api_key"])
    slug, token = _live_market(client, agent.api_key)
    desk = _desk()
    stale = OrderBookSummary(
        market="m", asset_id=token, timestamp="0", hash="h", asks=[OrderBookLevel(price="0.4", size="10")]
    )
    monkeypatch.setattr(desk._orders, "get_book", lambda _token: stale)

    result = desk.trade(agent, slug, "YES", "buy", shares=10)

    assert result.model_dump() == {
        "order_id": None, "status": "unfilled", "filled_shares": 0, "avg_price": None, "usd": 0, "resting_shares": 0
    }


def test_insufficient_cash_states_have_and_need():
    desk, agent, slug = _asks()

    with pytest.raises(InsufficientBalanceError, match=r"need \$500,000\.00, cash is \$100,000\.00"):
        desk.trade(agent, slug, "YES", "buy", shares=1_000_000, limit_price=0.5)


def test_portfolio_matches_the_leaderboard():
    desk, agent, slug = _asks()
    desk.trade(agent, slug, "YES", "buy", usd=30)
    desk.trade(agent, slug, "YES", "buy", shares=10, limit_price=0.1)
    desk._board.take_snapshot(int(time.time()))

    mine = desk.portfolio(agent)
    board = desk.leaderboard()
    standing = next(s for s in board.agents if s.agent == mine.agent)

    assert mine.cash_usd == 100_000 - 20
    assert mine.equity_usd == pytest.approx(mine.cash_usd + mine.positions_value_usd)
    assert (mine.equity_usd, mine.pnl_usd, mine.rank) == (standing.equity_usd, standing.pnl_usd, standing.rank)
    assert mine.ranked_agents == board.total
    assert [(h.market, h.outcome, h.shares, h.avg_price) for h in mine.positions] == [(slug, "YES", 50, 0.4)]
    assert [(o.market, o.side, o.price, o.shares) for o in mine.open_orders] == [(slug, "buy", 0.1, 10)]
    assert mine.next_top_up_at is None


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
        user_id, _, api_key = TableWrite.create_user(
            conn, email=None, password_hash=None, handle="claude_bot", owner_workos_id="user_o", agent_app="Claude"
        )
        conn.execute(
            "INSERT INTO trades (TRADE_ID, TAKER_API_KEY, MATCH_TIME, STATUS) VALUES ('t1', %s, 0, 'CONFIRMED')",
            (api_key,),
        )
        TableWrite.insert_account_snapshot(conn, user_id, 1, 110_000_000_000, 100_000_000_000)

    board = _desk().leaderboard()

    assert [(s.agent, s.app, s.return_pct, s.pnl_usd) for s in board.agents] == [("claude_bot", "Claude", 10, 10_000)]
