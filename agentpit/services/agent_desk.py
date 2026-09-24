import time
from datetime import UTC, datetime
from decimal import Decimal

from agentpit.config import Settings
from agentpit.datastructures.agent_desk import (
    Amount,
    BoardLimit,
    CancelResult,
    Holding,
    Leaderboard,
    MarketCard,
    MarketDetail,
    MarketList,
    OutcomeBook,
    Portfolio,
    Price,
    Quote,
    RestingOrder,
    SearchLimit,
    Side,
    Standing,
    TopUp,
    TradeResult,
)
from agentpit.datastructures.market import Market
from agentpit.datastructures.market_state import MarketState
from agentpit.datastructures.match_leg import MICRO, legs_for_user
from agentpit.datastructures.orderbook_summary import OrderBookLevel
from agentpit.datastructures.place_order_request import PlaceOrderRequest
from agentpit.datastructures.user import User
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.domain.exceptions import (
    BusinessRuleError,
    InsufficientBalanceError,
    MarketStateError,
    NotFoundError,
    OrderNotFilledError,
)
from agentpit.domain.text import clean
from agentpit.onchain.admin import OnchainAdmin
from agentpit.polymarket.format import decimal_str_to_price_int, decimal_str_to_size_micro
from agentpit.services.account_service import MIN_PRICE_MICRO, SLIPPAGE_CAP_MICRO, AccountService
from agentpit.services.balance_service import BalanceService
from agentpit.services.leaderboard_service import (
    LeaderboardService,
    compute_return_pct,
    display_name,
    rank_rows,
)
from agentpit.services.order_service import OrderService

TICK = 1_000
LOT = 10_000
TOP = 20
DEPTH = 5


def shares_for_usd(levels: list[tuple[int, int]], usd: int, limit: int, buy: bool) -> tuple[int, int]:
    shares = spent = 0
    for price, size in levels:
        if (price > limit) if buy else (price < limit):
            break
        take = min(size, (usd - spent) * MICRO // price)
        shares += take
        spent += take * price // MICRO
        if take < size:
            break
    return shares, spent


def snap(price: int, buy: bool) -> int:
    return price // TICK * TICK if buy else -(-price // TICK) * TICK


def _usd(micro: int) -> float:
    return _round2(micro / MICRO)


def _round2(value: float) -> float:
    return round(value, 2) + 0.0


def _px(micro: int) -> float:
    return round(micro / MICRO, 3)


def _shares(micro: int) -> float:
    return round(micro / MICRO, 6)


def _when(ts: int | None) -> datetime | None:
    return datetime.fromtimestamp(ts, UTC) if ts else None


def _quote(name: str, top: tuple[int | None, int | None]) -> Quote:
    bid, ask = top
    return Quote(name=name, bid=None if bid is None else _px(bid), ask=None if ask is None else _px(ask))


def _levels(levels: list[OrderBookLevel]) -> list[tuple[int, int]]:
    return [(decimal_str_to_price_int(l.price), decimal_str_to_size_micro(l.size)) for l in levels]


class AgentDesk:
    def __init__(self, db: DbSession, onchain: OnchainAdmin, settings: Settings) -> None:
        self._db = db
        self._onchain = onchain
        self._settings = settings
        self._accounts = AccountService(db, onchain)
        self._orders = OrderService(db, onchain)
        self._balance = BalanceService(db, onchain, settings, self._accounts)
        self._board = LeaderboardService(db, onchain, self._accounts, settings)

    def search_markets(self, query: str | None = None, limit: SearchLimit = 10) -> MarketList:
        with self._db.read() as conn:
            found = TableRead.search_live_markets(
                conn,
                query=query,
                limit=limit,
                excluded_categories=self._settings.excluded_categories,
                excluded_tags=self._settings.excluded_tags,
            )
            tops = TableRead.book_tops_for_tokens(conn, [t for m in found for t, _ in m.erc1155_tokens])
        return MarketList(
            markets=[
                MarketCard(
                    market=m.slug,
                    question=clean(m.question, 200),
                    closes_at=_when(m.end_date),
                    outcomes=[_quote(label, tops.get(token, (None, None))) for token, label in m.erc1155_tokens],
                )
                for m in found
            ]
        )

    def get_market(self, market: str) -> MarketDetail:
        m = self._market(market)
        outcomes: list[OutcomeBook] = []
        for token, label in m.erc1155_tokens:
            book = self._orders.get_book(token)
            bids, asks = _levels(book.bids), _levels(book.asks)
            history = self._orders.get_prices_history(token, interval="1d")["history"]
            outcomes.append(
                OutcomeBook(
                    name=label,
                    bid=_px(bids[0][0]) if bids else None,
                    ask=_px(asks[0][0]) if asks else None,
                    last=None if book.last_trade_price == "0" else float(book.last_trade_price),
                    change_1d=round(history[-1]["p"] - history[0]["p"], 3) if len(history) > 1 else None,
                    bids=[(_px(p), _shares(s)) for p, s in bids[:DEPTH]],
                    asks=[(_px(p), _shares(s)) for p, s in asks[:DEPTH]],
                )
            )
        return MarketDetail(
            market=m.slug,
            question=clean(m.question, 200),
            rules=clean(m.description, 1500),
            status=m.market_state.value.lower(),
            closes_at=_when(m.end_date),
            winner=m.erc1155_tokens[m.resolved_outcome][1] if m.resolved_outcome is not None else None,
            outcomes=outcomes,
        )

    def trade(
        self,
        user: User,
        market: str,
        outcome: str,
        side: Side,
        usd: Amount | None = None,
        shares: Amount | None = None,
        limit_price: Price | None = None,
    ) -> TradeResult:
        m = self._market(market)
        if m.market_state != MarketState.ACTIVE:
            raise MarketStateError(f"'{market}' is {m.market_state.value.lower()}, so it cannot be traded.")
        token, label = self._outcome(m, outcome)
        buy = side == "buy"
        if limit_price is None:
            book = self._orders.get_book(token)
            levels = _levels(book.asks if buy else book.bids)
            if not levels:
                raise BusinessRuleError(
                    f"No {'asks' if buy else 'bids'} for '{label}' in '{market}' right now. "
                    "Pass a limit_price to rest an order at your price."
                )
            best = levels[0][0]
            edge = best + SLIPPAGE_CAP_MICRO if buy else best - SLIPPAGE_CAP_MICRO
            limit = snap(min(max(edge, MIN_PRICE_MICRO), MICRO - MIN_PRICE_MICRO), buy)
            if (best > limit) if buy else (best < limit):
                raise BusinessRuleError(
                    f"Best {'ask' if buy else 'bid'} for '{label}' is {_px(best)}, outside the tradable "
                    f"range (limit {_px(limit)}). Pass a limit_price to rest an order at your price."
                )
        else:
            levels = []
            limit = snap(round(limit_price * MICRO), buy)
        unspent = 0
        if shares is not None and usd is None:
            size = round(shares * MICRO)
        elif usd is not None and shares is None:
            cash = round(usd * MICRO)
            if limit_price is None:
                size, spent = shares_for_usd(levels, cash, limit, buy)
                unspent = cash - spent
            else:
                size = cash * MICRO // limit
        else:
            raise BusinessRuleError("Give exactly one of usd or shares.")
        size = size // LOT * LOT
        if size == 0:
            raise BusinessRuleError(f"That is less than 0.01 share at {_px(limit)}. Increase usd or shares.")
        request = PlaceOrderRequest(
            token_id=token,
            side="BUY" if buy else "SELL",
            price=Decimal(limit) / MICRO,
            size=Decimal(size) / MICRO,
            order_type="FAK" if limit_price is None else "GTC",
        )
        try:
            placed = self._orders.place_order(user, request)
        except OrderNotFilledError:
            return TradeResult(order_id=None, status="unfilled", filled_shares=0, avg_price=None, usd=0, resting_shares=0)
        except InsufficientBalanceError:
            raise self._shortfall(user, token, label, buy, limit, size) from None
        if not placed.success:
            rest = f"; order {placed.orderID} is still resting, cancel it if unwanted" if placed.status == "live" else ""
            raise BusinessRuleError(f"settlement failed, nothing was traded{rest}")
        with self._db.read() as conn:
            rows = TableRead.list_trades_for_api_key(conn, user.api_key, taker_order_id=placed.orderID)
        legs = [leg for r in rows for leg in legs_for_user(r, user.api_key) if leg.is_taker]
        filled = sum(leg.size_micro for leg in legs)
        cost = sum(leg.price_micro * leg.size_micro for leg in legs)
        resting = size - filled if placed.status == "live" else 0
        return TradeResult(
            order_id=placed.orderID,
            status="resting" if resting else "partial" if filled < size or unspent > LOT else "filled",
            filled_shares=_shares(filled),
            avg_price=round(cost / filled / MICRO, 3) if filled else None,
            usd=_usd(cost // MICRO),
            resting_shares=_shares(resting),
        )

    def cancel(self, user: User, order_id: str | None = None) -> CancelResult:
        done = self._orders.cancel_orders(user, [order_id]) if order_id else self._orders.cancel_all(user)
        return CancelResult(cancelled=len(done.canceled))

    def portfolio(self, user: User) -> Portfolio:
        cash = self._onchain.usd_balance(user.eth_address)
        held = sorted(self._accounts.list_positions(user.eth_address), key=lambda p: -p.currentValue)
        value = round(sum(p.currentValue for p in held) * MICRO)
        equity = cash + value
        with self._db.read() as conn:
            deposited = TableRead.get_total_deposited(conn, user.user_id, self._settings.paper_balance_target_raw)
        board = rank_rows(self._board.build_board(), "return")
        next_at = self._balance.next_allowed(user)
        orders = self._orders.list_open_orders(user)
        shown = orders[:TOP]
        slugs: dict[str, str] = {}
        if shown:
            with self._db.read() as conn:
                found = TableRead.list_markets_filtered(conn, condition_ids=list({o.market for o in shown}), limit=TOP)
            slugs = {m.condition_id.value: m.slug for m in found}
        return Portfolio(
            agent=display_name(user.handle, user.eth_address),
            app=user.agent_app,
            cash_usd=_usd(cash),
            positions_value_usd=_usd(value),
            equity_usd=_usd(equity),
            pnl_usd=_usd(equity - deposited),
            return_pct=_round2(compute_return_pct(equity, deposited)),
            rank=next((i + 1 for i, r in enumerate(board) if r.address == user.eth_address), None),
            ranked_agents=len(board),
            next_top_up_at=_when(next_at if next_at > time.time() else None),
            positions=[
                Holding(
                    market=p.slug,
                    outcome=p.outcome,
                    shares=round(p.size, 6),
                    avg_price=round(p.avgPrice, 3),
                    price=round(p.curPrice, 3),
                    value_usd=round(p.currentValue, 2),
                    pnl_usd=round(p.cashPnl, 2),
                )
                for p in held[:TOP]
            ],
            positions_total=len(held),
            open_orders=[
                RestingOrder(
                    order_id=o.id,
                    market=slugs[o.market],
                    outcome=o.outcome,
                    side="buy" if o.side == "BUY" else "sell",
                    price=float(o.price),
                    shares=_shares(decimal_str_to_size_micro(o.original_size) - decimal_str_to_size_micro(o.size_matched)),
                )
                for o in shown
            ],
            open_orders_total=len(orders),
        )

    def top_up(self, user: User) -> TopUp:
        now = int(time.time())
        result = self._balance.top_up(user, now)
        return TopUp(
            added_usd=_usd(result.minted_raw),
            equity_usd=_usd(result.balance_raw),
            next_top_up_at=_when(result.next_allowed_at if result.next_allowed_at > now else None),
        )

    def leaderboard(self, limit: BoardLimit = 10) -> Leaderboard:
        board = rank_rows(self._board.build_board(), "return")
        return Leaderboard(
            agents=[
                Standing(
                    rank=i + 1,
                    agent=r.name,
                    app=r.app,
                    return_pct=_round2(r.return_pct),
                    pnl_usd=_usd(r.earned_raw),
                    equity_usd=_usd(r.capital_raw),
                    trades=r.trades,
                )
                for i, r in enumerate(board[:limit])
            ],
            total=len(board),
        )

    def _market(self, slug: str) -> Market:
        with self._db.read() as conn:
            found = TableRead.list_markets_filtered(conn, slug=slug, limit=1)
        if not found:
            raise NotFoundError(f"No market '{slug}'. Use a market slug exactly as search_markets returns it.")
        return found[0]

    @staticmethod
    def _outcome(market: Market, name: str) -> tuple[str, str]:
        for token, label in market.erc1155_tokens:
            if label.casefold() == name.casefold():
                return token, label
        names = ", ".join(f"'{label}'" for _, label in market.erc1155_tokens)
        raise NotFoundError(f"'{market.slug}' has outcomes {names}; got '{name}'.")

    def _shortfall(self, user: User, token: str, label: str, buy: bool, limit: int, size: int) -> InsufficientBalanceError:
        if buy:
            have = self._onchain.usd_balance(user.eth_address)
            return InsufficientBalanceError(
                f"Not enough cash: {_shares(size)} shares at up to {_px(limit)} need ${_usd(limit * size // MICRO):,.2f}, "
                f"cash is ${_usd(have):,.2f}. Trade less, or call top_up if equity is below $100,000 and portfolio shows no next_top_up_at."
            )
        have = self._onchain.ctf_balance(user.eth_address, int(token))
        return InsufficientBalanceError(
            f"Not enough shares: you hold {_shares(have)} '{label}', this sell needs {_shares(size)}."
        )
