import json

from agentpit.datastructures.match_leg import legs_for_user
from agentpit.datastructures.trade_wire import (
    MakerOrderWire,
    TradesEnvelope,
    TradeWire,
)
from agentpit.datastructures.user import User
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.polymarket.format import price_to_decimal_str, size_to_decimal_str
from agentpit.polymarket.resolve import resolve_by_token_id


def _status(internal: str) -> str:
    return "FAILED" if internal == "FAILED" else "MATCHED"


class TradeService:
    def __init__(self, db: DbSession):
        self._db = db

    def list_trades(self, user: User, *, limit: int = 100, **filters) -> TradesEnvelope:
        with self._db.read() as conn:
            rows = TableRead.list_trades_for_api_key(
                conn, user.api_key, limit=limit, **filters
            )
            outcome_cache: dict[str, str] = {}
            trades = [
                TradeService._to_wire(
                    conn, r, api_key=user.api_key, user_id=user.user_id,
                    eth_address=user.eth_address,
                    prefer_token=filters.get("asset_id"),
                    outcome_cache=outcome_cache,
                )
                for r in rows
            ]
        # next_cursor is the static "no more pages" sentinel ("LTE="): agentpit
        # returns all matching trades up to `limit` in one page (paper-rig
        # volumes are small). TODO: real cursor pagination if counts grow.
        # The limit is now pushed into SQL (see list_trades_for_api_key), so
        # `rows`/`trades` already IS the page — this slice is a no-op safety
        # net, not the primary limiting mechanism.
        page = trades[:limit]
        return TradesEnvelope(limit=limit, count=len(page), data=page)

    @staticmethod
    def _to_wire(
        conn, r, *, api_key: str, user_id: str, eth_address: str,
        prefer_token: str | None = None,
        outcome_cache: dict[str, str] | None = None,
    ) -> TradeWire:
        trader_side = "TAKER" if r["TAKER_API_KEY"] == api_key else "MAKER"
        legs = legs_for_user(r, api_key)
        # No-filter behaviour (prefer_token=None): one row per trade, taker
        # leg preferred — unchanged. This endpoint is per-trade, not per-leg
        # (unlike the Activity feed), so a self-matched row's other leg is
        # deliberately not emitted here even though `legs` may hold both.
        # When the caller filtered by asset_id (prefer_token set), honor
        # that token if this row actually has a leg on it — a self-matched
        # MINT/MERGE can be selected via either branch, and returning the
        # OTHER token's leg would silently violate the caller's own filter.
        leg = None
        if prefer_token is not None:
            leg = next((x for x in legs if x.token_id == prefer_token), None)
        if leg is None:
            leg = next(
                (x for x in legs if x.is_taker == (trader_side == "TAKER")), legs[0]
            )
        makers_raw = json.loads(r["MAKER_ORDERS"]) if r["MAKER_ORDERS"] else []
        maker_orders = [
            MakerOrderWire(
                order_id=m["order_id"],
                owner=m.get("owner", ""),               # counterparty USER_ID
                maker_address=m.get("maker_address", ""),
                matched_amount=size_to_decimal_str(int(m["matched_amount"])),
                price=price_to_decimal_str(int(m["price"])),
                fee_rate_bps=str(m.get("fee_rate_bps", 0)),
                asset_id=m.get("asset_id", r["ASSET_ID"]),
                outcome=m.get("outcome", ""),
                side=m.get("side", ""),
            )
            for m in makers_raw
        ]
        # The outcome label follows the token THIS leg moved. It used to come
        # from maker_orders[0] for both perspectives, which handed a MINT's
        # taker the complement's label.
        if outcome_cache is not None and leg.token_id in outcome_cache:
            outcome = outcome_cache[leg.token_id]
        else:
            resolved = resolve_by_token_id(conn, leg.token_id)
            outcome = (
                resolved.market.erc1155_tokens[resolved.outcome_index][1]
                if resolved and resolved.market else ""
            )
            if outcome_cache is not None:
                outcome_cache[leg.token_id] = outcome
        match_time = str(int(r["MATCH_TIME"]))
        return TradeWire(
            id=r["TRADE_ID"],
            taker_order_id=r["TAKER_ORDER_ID"],
            market=r["MARKET"],
            asset_id=leg.token_id,
            side=leg.side,
            size=size_to_decimal_str(int(r["TRADE_SIZE"])),
            fee_rate_bps=str(r["FEE_RATE_BPS"]),
            price=price_to_decimal_str(leg.price_micro),
            status=_status(r["STATUS"]),
            match_time=match_time,
            last_update=match_time,
            outcome=outcome,
            bucket_index=int(r["BUCKET_INDEX"]),
            owner=user_id,
            maker_address=eth_address,
            maker_orders=maker_orders,
            transaction_hash=r["TRANSACTION_HASH"] or "",
            trader_side=trader_side,
        )
