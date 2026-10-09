import json
from dataclasses import dataclass

from agentpit.datastructures.activity_wire import ActivityWire
from agentpit.datastructures.market import Market
from agentpit.datastructures.market_state import MarketState
from agentpit.datastructures.match_leg import MICRO, legs_for_user
from agentpit.datastructures.position_wire import PositionWire
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.liquidity import feed
from agentpit.onchain.admin import OnchainAdmin
from agentpit.polymarket.format import price_to_float, size_to_float
from agentpit.polymarket.resolve import ResolvedOutcome, resolve_by_token_id


@dataclass(frozen=True)
class _TokenFlow:
    """What a user's fills of one outcome token add up to.

    Sizes are micro-shares and costs are price-micro x size-micro, so a ratio
    of the two is a micro-price and needs no intermediate rounding.
    """

    bought_size: int
    bought_cost: int
    sold_size: int
    sold_proceeds: int
    last_sell_time: int

    @property
    def net_size(self) -> int:
        return self.bought_size - self.sold_size

    @property
    def avg_buy_price_micro(self) -> int:
        """Size-weighted micro-price paid per share; 0 when nothing was bought."""
        return self.bought_cost // self.bought_size if self.bought_size else 0


#: A market sell is a GTC limit for the whole size at
#: `max(best_bid - SLIPPAGE_CAP, MIN_PROB)` with the unfilled remainder
#: cancelled -- `placeMarketOrder` in `ui/src/api/orders.ts` on top of
#: `computeMarketSell` in `ui/src/components/orders/orderMath.ts`. In micro
#: units here because that is what the `orders` rows carry.
SLIPPAGE_CAP_MICRO = 20_000
MIN_PRICE_MICRO = 10_000


@dataclass(frozen=True)
class Sellable:
    """What the live bids would pay for a position sold at market."""

    size: float   # shares they absorb; less than the position when thin
    value: float  # dollars they return


def sellable_against_bids(
    bids: list[tuple[int, int]], size_micro: int
) -> Sellable:
    """Simulate the market sell of `size_micro` shares against `bids`.

    `bids` is one `(price_micro, size_micro)` per bid level, in any order.
    The order the ticket sends cannot fill below its price cap, so levels
    under the floor are invisible to it however deep they are, and
    a position larger than the depth above the floor comes back short rather
    than resting on the book — the ticket cancels that remainder.
    """
    if size_micro <= 0 or not bids:
        return Sellable(0.0, 0.0)
    floor = max(max(p for p, _ in bids) - SLIPPAGE_CAP_MICRO, MIN_PRICE_MICRO)
    filled_micro = 0
    # Price-micro x size-micro, so the walk needs no intermediate rounding
    # (the same reason `_TokenFlow` carries its costs that way).
    proceeds = 0
    for price, available in sorted(bids, key=lambda b: -b[0]):
        if price < floor:
            break
        take = min(available, size_micro - filled_micro)
        if take <= 0:
            break
        filled_micro += take
        proceeds += price * take
    return Sellable(filled_micro / 1_000_000, proceeds / 1_000_000 / 1_000_000)


@dataclass(frozen=True)
class _LivePricing:
    """Everything one read of a token's live orders answers."""

    cur_price: float
    sellable: Sellable


class AccountService:
    """Public-by-address account reads (positions / value / activity)."""

    def __init__(self, db: DbSession, onchain: OnchainAdmin):
        self._db = db
        self._onchain = onchain

    def list_positions(
        self, eth_address: str, market: list[str] | None = None
    ) -> list[PositionWire]:
        with self._db.read() as conn:
            user = TableRead.get_user_by_eth_address(conn, eth_address)
            if user is None:
                return []
            # Only markets the user has traded or split can hold a CTF balance.
            # Scanning every market on-chain is O(market count) reads (~24s at
            # 1000 markets); scope to the handful the user actually touched.
            markets = TableRead.list_markets_with_user_activity(conn, user.api_key)
            event_slugs = TableRead.event_slugs_by_id(
                conn, [m.event_id for m in markets if m.event_id is not None]
            )
        # Narrow first, then ask the chain ONCE. A balance read costs about
        # half a second against a remote node, and this scan wants two tokens
        # per market the account has ever touched -- read one at a time, a
        # 500-trade account spent 23 seconds here for fourteen rows.
        scanned = [
            (mkt, idx, token_id, label)
            for mkt in markets
            if not market or mkt.condition_id.value in market
            for idx, (token_id, label) in enumerate(mkt.erc1155_tokens)
        ]
        balances = self._onchain.ctf_balances(
            eth_address, [int(t[2]) for t in scanned]
        )

        out: list[PositionWire] = []
        # One connection for the whole build, not one per token: the pricing
        # reads below are small, and acquiring a pooled connection per token
        # was the second cost in this loop.
        with self._db.read() as conn:
            for (mkt, idx, token_id, label), bal in zip(scanned, balances):
                if bal <= 0:
                    continue
                tokens = mkt.erc1155_tokens
                size = bal / 1_000_000
                redeemable = mkt.payouts is not None and mkt.payouts[idx] > 0
                avg_price = self._avg_fill_price(conn, user.api_key, token_id)
                settled = mkt.market_state == MarketState.RESOLVED
                # A resolved market has no live book any more, so
                # `_live_pricing` would fall through to the last trade print
                # and show settled money at whatever it last changed hands
                # for -- and nothing is sellable, for want of anything to
                # sell into.
                opp_idx = 1 - idx if len(tokens) == 2 else idx
                opp_token, opp_label = (
                    tokens[opp_idx] if len(tokens) == 2 else (token_id, label)
                )
                if mkt.payouts is not None:
                    cur_price = mkt.payouts[idx] / sum(mkt.payouts)
                    sellable = Sellable(0.0, 0.0)
                else:
                    pricing = self._live_pricing(conn, token_id, opp_token, bal)
                    cur_price = pricing.cur_price
                    sellable = pricing.sellable
                initial_value = avg_price * size
                current_value = cur_price * size
                cash_pnl = current_value - initial_value
                pct_pnl = (cash_pnl / initial_value * 100) if initial_value else 0.0
                out.append(
                    PositionWire(
                        proxyWallet=eth_address,
                        asset=token_id,
                        conditionId=mkt.condition_id.value,
                        size=size,
                        avgPrice=avg_price,
                        initialValue=initial_value,
                        currentValue=current_value,
                        cashPnl=cash_pnl,
                        percentPnl=pct_pnl,
                        totalBought=initial_value,
                        curPrice=cur_price,
                        sellableValue=sellable.value,
                        sellableSize=sellable.size,
                        settled=settled,
                        redeemable=redeemable,
                        title=mkt.question,
                        slug=mkt.slug or "",
                        icon=mkt.icon_url or "",
                        outcome=label,
                        outcomeIndex=idx,
                        oppositeOutcome=opp_label,
                        oppositeAsset=opp_token,
                        eventSlug=event_slugs.get(mkt.event_id or -1, ""),
                        url=mkt.url,
                        endDate=str(mkt.end_date) if mkt.end_date else "",
                    )
                )
        return out

    def list_closed_positions(self, eth_address: str) -> list[PositionWire]:
        """Won positions on resolved markets, reconstructed from REDEEM payouts
        (the Active /positions list drops them once redeemed — token balance 0).

        Payout is the REDEEM collateral (ground truth, robust to MINT/MERGE,
        unlike per-token trade nets); cost basis is the user's net USDC into the
        market across both outcomes; realized PnL = payout - cost."""
        with self._db.read() as conn:
            user = TableRead.get_user_by_eth_address(conn, eth_address)
            if user is None:
                return []
            redeem_rows = conn.execute(
                "SELECT MARKET_ID, DETAILS FROM transactions "
                "WHERE API_KEY = %s AND TRANSACTION_TYPE = 'REDEEM'",
                (user.api_key,),
            ).fetchall()
            # MAX payout per market: a position redeems once, so duplicate redeem
            # logs are deduped by max rather than summed.
            payout_micro: dict[int, int] = {}
            for r in redeem_rows:
                if r["MARKET_ID"] is None:
                    continue
                details = json.loads(r["DETAILS"]) if r["DETAILS"] else {}
                mid = int(r["MARKET_ID"])
                payout_micro[mid] = max(
                    payout_micro.get(mid, 0),
                    int(details.get("collateral_amount", 0)),
                )

            out: list[PositionWire] = []
            redeemed = {
                mid: mkt
                for mid in payout_micro
                if (mkt := TableRead.read_market(conn, mid)) is not None
            }
            event_slugs = TableRead.event_slugs_by_id(
                conn, [m.event_id for m in redeemed.values() if m.event_id is not None]
            )
            for market_id, payout in payout_micro.items():
                mkt = redeemed.get(market_id)
                if mkt is None or mkt.payouts is None:
                    continue
                tokens = mkt.erc1155_tokens
                if len(tokens) != 2:
                    continue  # binary markets only for now
                payouts = mkt.payouts
                if payouts[0] == payouts[1]:
                    pos_idx = max((0, 1), key=lambda i: self._net_bought(conn, user.api_key, tokens[i][0]))
                else:
                    pos_idx = payouts.index(max(payouts) if payout > 0 else min(payouts))
                cur_price = payouts[pos_idx] / sum(payouts)
                if payout > 0:
                    size = size_to_float(payout) / cur_price
                    value = payout / 1_000_000
                else:
                    # Lost: held the losing outcome (redeemed for $0). Size comes
                    # from the user's net buys of that token.
                    net = self._net_bought(conn, user.api_key, tokens[pos_idx][0])
                    if net <= 0:
                        continue
                    size = size_to_float(net)
                    value = 0.0
                token_id = tokens[pos_idx][0]
                avg = self._avg_fill_price(conn, user.api_key, token_id)
                cost = avg * size
                pnl = value - cost
                pct = (pnl / cost * 100) if cost else 0.0
                opp_idx = 1 - pos_idx
                out.append(
                    PositionWire(
                        proxyWallet=eth_address,
                        asset=token_id,
                        conditionId=mkt.condition_id.value,
                        size=size,
                        avgPrice=avg,
                        initialValue=cost,
                        currentValue=value,
                        cashPnl=pnl,
                        percentPnl=pct,
                        totalBought=cost,
                        curPrice=cur_price,
                        redeemable=False,
                        title=mkt.question,
                        slug=mkt.slug or "",
                        icon=mkt.icon_url or "",
                        outcome=tokens[pos_idx][1],
                        outcomeIndex=pos_idx,
                        oppositeOutcome=tokens[opp_idx][1],
                        oppositeAsset=tokens[opp_idx][0],
                        eventSlug=event_slugs.get(mkt.event_id or -1, ""),
                        url=mkt.url,
                        endDate=str(mkt.end_date) if mkt.end_date else "",
                    )
                )
            out.extend(
                self._sold_out_positions(
                    conn, eth_address, user.api_key, skip_market_ids=set(payout_micro)
                )
            )
        return out

    def _sold_out_positions(
        self, conn, eth_address: str, api_key: str, *, skip_market_ids: set[int]
    ) -> list[PositionWire]:
        """Positions the user closed by SELLING every share back.

        Such an exit leaves no REDEEM to reconstruct from and no token balance
        for the Active list to find, so without this the position — and its
        realized profit — disappears from the Closed tab, the predictions
        count, Biggest Win and the P/L chart at once.

        Markets that produced a redeem are skipped: that reconstruction already
        owns them. The two cannot both apply anyway — redeeming needs tokens,
        and a market whose fills net to zero has none left.
        """
        out: list[PositionWire] = []
        candidates = [
            m
            for m in TableRead.list_markets_with_user_activity(conn, api_key)
            if m.market_id not in skip_market_ids
        ]
        event_slugs = TableRead.event_slugs_by_id(
            conn, [m.event_id for m in candidates if m.event_id is not None]
        )
        for mkt in candidates:
            tokens = mkt.erc1155_tokens
            for idx, (token_id, label) in enumerate(tokens):
                flow = self._token_flow(conn, api_key, token_id)
                # Sold out exactly: bought something, sold it all back. A
                # partial exit still holds tokens and belongs in Active, where
                # its remaining size and live price are the interesting numbers.
                if flow.sold_size <= 0 or flow.net_size != 0:
                    continue
                size = size_to_float(flow.sold_size)
                avg = price_to_float(flow.avg_buy_price_micro)
                cost = avg * size
                proceeds = flow.sold_proceeds / 1_000_000 / 1_000_000
                pnl = proceeds - cost
                pct = (pnl / cost * 100) if cost else 0.0
                avg_sell = flow.sold_proceeds // flow.sold_size
                opp_idx = 1 - idx if len(tokens) == 2 else idx
                opp_token, opp_label = (
                    tokens[opp_idx] if len(tokens) == 2 else (token_id, label)
                )
                out.append(
                    PositionWire(
                        proxyWallet=eth_address,
                        asset=token_id,
                        conditionId=mkt.condition_id.value,
                        size=size,
                        avgPrice=avg,
                        initialValue=cost,
                        currentValue=proceeds,
                        cashPnl=pnl,
                        percentPnl=pct,
                        totalBought=cost,
                        realizedPnl=pnl,
                        percentRealizedPnl=pct,
                        curPrice=price_to_float(avg_sell),
                        redeemable=False,
                        title=mkt.question,
                        slug=mkt.slug or "",
                        icon=mkt.icon_url or "",
                        outcome=label,
                        outcomeIndex=idx,
                        oppositeOutcome=opp_label,
                        oppositeAsset=opp_token,
                        eventSlug=event_slugs.get(mkt.event_id or -1, ""),
                        url=mkt.url,
                        # The sale, not the market's end. This field is what the
                        # P/L chart plots a closed position at, and an unresolved
                        # market's end date is still in the future — it would put
                        # today's realized profit ahead of today.
                        endDate=str(flow.last_sell_time),
                    )
                )
        return out

    @staticmethod
    def _net_bought(conn, api_key: str, token_id: str) -> int:
        """Net outcome tokens the user bought of `token_id` (effective buys minus
        sells; a maker fills the side opposite the stored taker SIDE)."""
        return AccountService._token_flow(conn, api_key, token_id).net_size

    @staticmethod
    def _token_flow(conn, api_key: str, token_id: str) -> "_TokenFlow":
        """Everything the user's fills of one token add up to, in one pass.

        Reconstructing a position that was closed by SELLING needs the sale
        proceeds and the sale time, not just the net quantity `_net_bought`
        reports, and it must not double-count a maker fill as the taker's side.

        A trade row's stored PRICE is always the MAKER's price; which token
        moved and in which direction depends on `MATCH_KIND` — the truth
        table lives in `agentpit.datastructures.match_leg`, not here.

        For a MINT/MERGE, both parties transact in different tokens whose
        prices sum to the $1 the mint costs (or the merge returns), so a
        query scoped to `token_id` must match on ASSET_ID for the taker leg
        and on MAKER_ASSET_ID for the maker leg.

        The matcher has no same-account guard, so one row can have this user
        as BOTH taker and maker (a resting order of theirs crossed by a later
        order of theirs). Both legs are real and are booked independently —
        `legs_for_user` returns one entry per leg the user holds — rather
        than picking one via a single `is_taker`.
        """
        rows = conn.execute(
            "SELECT SIDE, PRICE, TRADE_SIZE, MATCH_TIME, MATCH_KIND, "
            "ASSET_ID, MAKER_ASSET_ID, TAKER_API_KEY, MAKER_API_KEY "
            "FROM trades WHERE STATUS != 'FAILED' "
            "AND ((TAKER_API_KEY = %s AND ASSET_ID = %s) "
            "  OR (MAKER_API_KEY = %s AND COALESCE(MAKER_ASSET_ID, ASSET_ID) = %s))",
            (api_key, token_id, api_key, token_id),
        ).fetchall()
        bought_size = bought_cost = sold_size = sold_proceeds = 0
        last_sell_time = 0
        for r in rows:
            for leg in legs_for_user(r, api_key):
                if leg.token_id != token_id:
                    continue
                if leg.side == "BUY":
                    bought_size += leg.size_micro
                    bought_cost += leg.price_micro * leg.size_micro
                else:
                    sold_size += leg.size_micro
                    sold_proceeds += leg.price_micro * leg.size_micro
                    last_sell_time = max(last_sell_time, int(r["MATCH_TIME"] or 0))
        return _TokenFlow(
            bought_size=bought_size,
            bought_cost=bought_cost,
            sold_size=sold_size,
            sold_proceeds=sold_proceeds,
            last_sell_time=last_sell_time,
        )

    def total_value(self, eth_address: str) -> list[dict]:
        value = sum(p.currentValue for p in self.list_positions(eth_address))
        return [{"user": eth_address, "value": value}]

    def list_activity(
        self,
        eth_address: str,
        *,
        type_filter: list[str] | None = None,
        market: list[str] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ActivityWire]:
        cap = None if type_filter or market else offset + limit
        with self._db.read() as conn:
            user = TableRead.get_user_by_eth_address(conn, eth_address)
            if user is None:
                return []
            acts: list[ActivityWire] = AccountService._trade_activity(
                conn, user.api_key, eth_address, cap
            )
            tx_rows = conn.execute(
                "SELECT TRANSACTION_TYPE, MARKET_ID, DETAILS, "
                "EXTRACT(EPOCH FROM TIMESTAMP)::bigint AS TS "
                "FROM transactions WHERE API_KEY = %s "
                "ORDER BY TIMESTAMP DESC LIMIT %s",
                (user.api_key, cap),
            ).fetchall()

            # One account fills the same handful of markets repeatedly, so the
            # slug of an event is looked up once and reused across its rows.
            slug_cache: dict[int, str] = {}

            def event_slug_of(mkt) -> str:
                if mkt is None or mkt.event_id is None:
                    return ""
                if mkt.event_id not in slug_cache:
                    found = TableRead.event_slugs_by_id(conn, [mkt.event_id])
                    slug_cache[mkt.event_id] = found.get(mkt.event_id, "")
                return slug_cache[mkt.event_id]

            markets: dict[int | None, Market | None] = {None: None}
            for r in tx_rows:
                market_id = r["MARKET_ID"]
                if market_id not in markets:
                    markets[market_id] = TableRead.read_market(conn, market_id)
                mkt = markets[market_id]
                details = json.loads(r["DETAILS"]) if r["DETAILS"] else {}
                amount = details.get("amount", details.get("collateral_amount", 0))
                size = (amount or 0) / 1_000_000
                acts.append(ActivityWire(
                    proxyWallet=eth_address,
                    timestamp=int(r["TS"]) if r["TS"] is not None else 0,
                    conditionId=mkt.condition_id.value if mkt else "",
                    type=r["TRANSACTION_TYPE"],
                    size=size,
                    usdcSize=size,
                    title=mkt.question if mkt else "",
                    slug=(mkt.slug or "") if mkt else "",
                    icon=(mkt.icon_url or "") if mkt else "",
                    eventSlug=event_slug_of(mkt),
                    url=mkt.url if mkt else None,
                ))

        if type_filter:
            acts = [a for a in acts if a.type in type_filter]
        if market:
            acts = [a for a in acts if a.conditionId in market]
        acts.sort(key=lambda a: a.timestamp, reverse=True)
        return acts[offset:offset + limit]

    @staticmethod
    def _trade_activity(
        conn, api_key: str, eth_address: str, limit: int | None = None
    ) -> "list[ActivityWire]":
        """One ActivityWire per leg this account holds.

        A NORMAL self-match yields two rows, a buy and a sell — that is the
        account genuinely standing on both sides. A MINT/MERGE maker's row
        names the token it actually received, not the taker's.
        """
        rows = conn.execute(
            "SELECT MARKET, ASSET_ID, MAKER_ASSET_ID, MATCH_KIND, SIDE, PRICE, "
            "TRADE_SIZE, MATCH_TIME, TRANSACTION_HASH, TAKER_API_KEY, "
            "MAKER_API_KEY FROM trades "
            "WHERE (TAKER_API_KEY = %s OR MAKER_API_KEY = %s) AND STATUS != 'FAILED' "
            "ORDER BY MATCH_TIME DESC LIMIT %s",
            (api_key, api_key, limit),
        ).fetchall()

        acts: list[ActivityWire] = []
        slug_cache: dict[int, str] = {}
        outcomes: dict[str, ResolvedOutcome | None] = {}

        def event_slug_of(mkt) -> str:
            if mkt is None or mkt.event_id is None:
                return ""
            if mkt.event_id not in slug_cache:
                found = TableRead.event_slugs_by_id(conn, [mkt.event_id])
                slug_cache[mkt.event_id] = found.get(mkt.event_id, "")
            return slug_cache[mkt.event_id]

        for r in rows:
            for leg in legs_for_user(r, api_key):
                # Resolve the token THIS leg moved, not the row's ASSET_ID:
                # they differ for a MINT/MERGE maker, and the outcome label
                # and index have to follow the corrected token.
                if leg.token_id not in outcomes:
                    outcomes[leg.token_id] = resolve_by_token_id(conn, leg.token_id)
                resolved = outcomes[leg.token_id]
                mkt = resolved.market if resolved else None
                price = price_to_float(leg.price_micro)
                size = size_to_float(leg.size_micro)
                outcome = (
                    mkt.erc1155_tokens[resolved.outcome_index][1]
                    if resolved and mkt else ""
                )
                acts.append(ActivityWire(
                    proxyWallet=eth_address,
                    timestamp=int(r["MATCH_TIME"] or 0),
                    conditionId=r["MARKET"],
                    type="TRADE",
                    size=size,
                    usdcSize=price * size,
                    transactionHash=r["TRANSACTION_HASH"] or "",
                    price=price,
                    asset=leg.token_id,
                    side=leg.side,
                    outcomeIndex=resolved.outcome_index if resolved else 0,
                    title=mkt.question if mkt else "",
                    slug=(mkt.slug or "") if mkt else "",
                    icon=(mkt.icon_url or "") if mkt else "",
                    eventSlug=event_slug_of(mkt),
                    url=mkt.url if mkt else None,
                    outcome=outcome,
                ))
        return acts

    # --- helpers --------------------------------------------------------

    @staticmethod
    def _avg_fill_price(conn, api_key: str, token_id: str) -> float:
        """Size-weighted price the user PAID per share of this asset, in dollars;
        0.0 if none.

        Delegates to `_token_flow`, which is the only place that resolves the
        user's effective side correctly. Filtering `SIDE = 'BUY'` here directly
        would not do it: SIDE is the TAKER's side while the row is reachable
        from either counterparty, so a resting ask of ours that got hit --
        our SELL -- reads as a taker BUY and would be folded into the price we
        supposedly paid.
        """
        return price_to_float(
            AccountService._token_flow(conn, api_key, token_id).avg_buy_price_micro
        )

    @staticmethod
    def _live_pricing(
        conn, token_id: str, opposite: str, size_micro: int
    ) -> "_LivePricing":
        """Book midpoint (fall back to the last print, then 1 - the opposite
        token's last print, else 0.5) AND what a market sell of `size_micro`
        shares would fetch, from ONE snapshot of Polymarket's book.
        """
        snap = feed.book(token_id)
        sellable = sellable_against_bids(list(snap.bids) if snap else [], size_micro)
        if snap is not None and snap.bids and snap.asks:
            mid = price_to_float((snap.bids[0][0] + snap.asks[0][0]) // 2)
            return _LivePricing(mid, sellable)
        lasts = TableRead.last_trade_prices_for_tokens(conn, [token_id, opposite])
        if token_id in lasts:
            return _LivePricing(price_to_float(lasts[token_id]), sellable)
        if opposite in lasts:
            return _LivePricing(price_to_float(MICRO - lasts[opposite]), sellable)
        return _LivePricing(0.5, sellable)
