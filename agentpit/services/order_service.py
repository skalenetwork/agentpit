import hashlib
import json
import logging
import secrets
import time

import psycopg
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from eth_utils.crypto import keccak
from web3 import Web3

from agentpit.datastructures.cancel_orders_response import CancelOrdersResponse
from agentpit.datastructures.orderbook_summary import OrderBookLevel, OrderBookSummary
from agentpit.common import check_state
from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.match import Match
from agentpit.datastructures.market_state import MarketState
from agentpit.datastructures.order_response import OrderResponse
from agentpit.datastructures.place_order_request import PlaceOrderRequest
from agentpit.datastructures.user import User
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.liquidity import feed
from agentpit.liquidity.feed import MarketRef
from agentpit.liquidity.replica import BookReplica
from agentpit.domain.exceptions import (
    BusinessRuleError,
    InsufficientBalanceError,
    MarketNotFoundError,
    MarketStateError,
    NotFoundError,
    OrderNotFilledError,
)
from agentpit.onchain.admin import OnchainAdmin
from agentpit.onchain.chain_rpc import Web3ChainRpc
from agentpit.onchain.order_signer import OrderData, sign_order
from agentpit.onchain.tx_sender import FORGET_DONE_AFTER_S, TxDropped, TxUnknown
from agentpit.datastructures.open_order import OpenOrder
from agentpit.polymarket.format import decimal_str_to_size_micro, price_to_decimal_str, price_to_float, size_to_decimal_str
from agentpit.polymarket.resolve import ResolvedOutcome, resolve_by_token_id
from agentpit.services.leaderboard_service import touch
from agentpit.services.market_service import market_lock

log = logging.getLogger(__name__)

_USDC_DECIMALS = 6
_USDC_SCALE = Decimal(10**_USDC_DECIMALS)
_PRICE_ONE = 10**_USDC_DECIMALS  # stored PRICE units that equal $1.00
_ZERO_ADDR = "0x0000000000000000000000000000000000000000"
_SETTLE_TIMEOUT_S = 60
_RECEIPT_CHECK_AFTER_S = 5

# The CTFExchange decides crossing on the EXACT order amounts, not our rounded
# stored PRICE: price = makerAmount*1e18/takerAmount (BUY) or
# takerAmount*1e18/makerAmount (SELL), floored, with ONE = 1e18. We replicate
# it bit-for-bit so a DB match can never trip the on-chain NotCrossing revert
# (which reverts the whole matchOrders batch). See vendor CalculatorHelper.sol.
_EXCHANGE_ONE = 10**18

# Polymarket rejects a GTD expiring sooner than this, and we match them: with
# the one-minute grace subtracted on read, anything closer would be an order
# that is already dead when it is placed.
#
# Documented, not folklore: Polymarket docs, page `trading/place-orders.mdx`,
# "GTD orders expire one minute before their stated expiration as a security
# threshold. To set an effective lifetime of N seconds, use `now + 60 + N`.
# In addition, the expiration must be at least 3 minutes in the future —
# orders expiring sooner are rejected."
_EXPIRY_MIN_LEAD_SECONDS = 180


def _exchange_price(maker_amount: int, taker_amount: int, side: str) -> int:
    """CalculatorHelper._calculatePrice — floored, scaled by 1e18."""
    if side == "BUY":
        return (maker_amount * _EXCHANGE_ONE) // taker_amount if taker_amount else 0
    return (taker_amount * _EXCHANGE_ONE) // maker_amount if maker_amount else 0


def _orders_cross(
    taker_maker: int,
    taker_taker: int,
    taker_side: str,
    maker_maker: int,
    maker_taker: int,
) -> bool:
    """CalculatorHelper.isCrossing against a BUY maker, over both orders' amounts."""
    if taker_taker == 0 or maker_taker == 0:
        return True
    pa = _exchange_price(taker_maker, taker_taker, taker_side)
    pb = _exchange_price(maker_maker, maker_taker, "BUY")
    return pa + pb >= _EXCHANGE_ONE if taker_side == "BUY" else pb >= pa


class OrderService:
    """Agent orders fill against the house at Polymarket's book; the operator
    (admin key) settles each fill with one `matchOrders` on the CTFExchange.
    """

    def __init__(self, db: DbSession, onchain: OnchainAdmin):
        self._db = db
        self._onchain = onchain

    # --- public API -----------------------------------------------------

    def place_order(self, user: User, payload: PlaceOrderRequest) -> OrderResponse:
        if payload.order_type == "GTD":
            earliest = int(time.time()) + _EXPIRY_MIN_LEAD_SECONDS
            if payload.expiration < earliest:
                raise BusinessRuleError(
                    "a GTD order must expire at least 3 minutes from now"
                )
        coid = payload.client_order_id
        if coid is not None:
            with self._db.read() as conn:
                existing = TableRead.get_idempotency_order_id(conn, user.api_key, coid)
                if existing is not None:
                    return self._build_replay_response(conn, existing)
        resolved = self._resolve_token(payload)
        token_id_int = int(resolved.token_id)
        size_micro = decimal_str_to_size_micro(str(payload.size))
        maker_amount, taker_amount = self._amounts_from_price_size(
            payload.side, payload.price, size_micro
        )

        # Pre-flight balance check — reject obvious losers before signing.
        self._check_balance(user.eth_address, payload.side, maker_amount, token_id_int)

        order = OrderData(
            salt=secrets.randbits(256),
            maker=user.eth_address,
            signer=user.eth_address,
            taker=_ZERO_ADDR,
            tokenId=token_id_int,
            makerAmount=maker_amount,
            takerAmount=taker_amount,
            expiration=int(payload.expiration),
            nonce=0,
            feeRateBps=0,
            side=0 if payload.side == "BUY" else 1,
            signatureType=0,
        )
        signature = sign_order(user.eth_key, self._onchain._client.deployment, order)

        order_id = self._compute_order_id(order)
        price_int = self._price_int(order)

        try:
            with market_lock(resolved.market.market_id):
                with self._db.write() as conn:
                    state = TableRead.get_market_state(conn, resolved.market.market_id)
                    if state != MarketState.ACTIVE:
                        raise MarketStateError(
                            f"'{resolved.market.slug}' is {state.value.lower()}, so it cannot be traded."
                        )
                    if coid is not None:
                        TableWrite.claim_idempotency_key(
                            conn,
                            api_key=user.api_key,
                            client_order_id=coid,
                            order_id=order_id,
                            created_at=int(time.time()),
                        )
                    self._insert_order(
                        conn,
                        api_key=user.api_key,
                        order=order,
                        order_id=order_id,
                        signature=signature,
                        price_int=price_int,
                        order_type=payload.order_type,
                    )
                    quote = feed.quote(resolved.token_id)
                    match = self._take(
                        conn, self._get_order_row(conn, order_id), order, quote
                    )
                    filled = 0 if match is None else match.size
                    if payload.order_type == "FOK" and filled < size_micro:
                        raise OrderNotFilledError(
                            "order couldn't be fully filled. FOK orders are fully filled or killed."
                        )
                    if payload.order_type == "FAK" and not filled:
                        raise OrderNotFilledError(
                            "no orders found to match with FAK order. FAK orders are partially "
                            "filled or killed if no match is found."
                        )
                if match is not None and quote is not None:
                    quote[2].use(user.api_key, match.takes)
        except psycopg.errors.UniqueViolation:
            # A concurrent request claimed this client_order_id first; the row is
            # committed by the time the violation fires, so replay its order. A
            # violation without a client_order_id can't be from the claim, so
            # re-raise rather than mis-replay against a NULL key.
            if coid is None:
                raise
            with self._db.read() as conn:
                existing = TableRead.get_idempotency_order_id(conn, user.api_key, coid)
                if existing is None:
                    raise
                return self._build_replay_response(conn, existing)

        error, tx_hash = "", None
        if match is not None:
            try:
                error, tx_hash = self._settle(order, signature, match, wait=True)
            finally:
                touch(user.eth_address)
        settled = match if match is not None and not error else None

        with self._db.read() as conn:
            row = self._get_order_row(conn, order_id)
        making_amount, taking_amount = (
            ("", "")
            if settled is None
            else self._fill_amounts(
                payload.side,
                settled.agent_amount if payload.side == "BUY" else settled.house_amount,
                settled.size,
            )
        )

        return OrderResponse(
            success=not error,
            errorMsg=error,
            orderID=order_id,
            status=row["STATUS"],
            transactionsHashes=[tx_hash] if settled is not None and tx_hash else [],
            takingAmount=taking_amount,
            makingAmount=making_amount,
            tradeIDs=[settled.trade_id] if settled is not None else [],
        )

    def sweep(self) -> None:
        if feed.HOUSE is None:
            return
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT ORDER_ID, API_KEY, TOKEN_ID, SIDE, PRICE, REMAINING_AMOUNT "
                f"FROM orders WHERE {TableRead.LIVE_ORDER} ORDER BY CREATED_AT",
                (int(time.time()),),
            ).fetchall()
        for row in rows:
            quote = feed.quote(row["TOKEN_ID"])
            if quote is None:
                continue
            ref, yes, rep = quote
            limit = int(row["PRICE"])
            if not rep.take(
                row["API_KEY"],
                (row["SIDE"] == "BUY") == yes,
                limit if yes else _PRICE_ONE - limit,
                int(row["REMAINING_AMOUNT"]),
            ):
                continue
            try:
                with market_lock(ref.market_id):
                    with self._db.write() as conn:
                        if (
                            TableRead.get_market_state(conn, ref.market_id)
                            != MarketState.ACTIVE
                        ):
                            continue
                        fresh = conn.execute(
                            "SELECT * FROM orders "
                            f"WHERE ORDER_ID = %s AND {TableRead.LIVE_ORDER} FOR UPDATE",
                            (row["ORDER_ID"], int(time.time())),
                        ).fetchone()
                        if fresh is None:
                            continue
                        agent_order, agent_signature = self._order_from_json(fresh)
                        match = self._take(conn, fresh, agent_order, quote)
                    if match is None:
                        continue
                    rep.use(fresh["API_KEY"], match.takes)
                self._settle(agent_order, agent_signature, match, wait=False)
                touch(fresh["MAKER"])
            except Exception:
                log.exception("sweep fill for order %s failed", row["ORDER_ID"])

    def list_open_orders(
        self,
        user: User,
        *,
        market: str | None = None,
        asset_id: str | None = None,
        order_id: str | None = None,
    ) -> list[OpenOrder]:
        """Return the caller's live orders as Polymarket OpenOrder[] (§8.3)."""
        clauses = ["API_KEY = %s"]
        params: list = [user.api_key]
        if asset_id is not None:
            clauses.append("TOKEN_ID = %s")
            params.append(asset_id)
        if order_id is not None:
            clauses.append("ORDER_ID = %s")
            params.append(order_id)
        # Last, per the positional-parameter rule: any clause added above
        # this line without touching both lists the same way still lines up.
        clauses.append(TableRead.LIVE_ORDER)
        params.append(int(time.time()))
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT ORDER_ID, TOKEN_ID, SIDE, PRICE, REMAINING_AMOUNT, MAKER, "
                "MAKER_AMOUNT, TAKER_AMOUNT, CREATED_AT, EXPIRATION, ORDER_TYPE "
                f"FROM orders WHERE {' AND '.join(clauses)} "
                "ORDER BY CREATED_AT DESC",
                params,
            ).fetchall()
            out: list[OpenOrder] = []
            for r in rows:
                resolved = resolve_by_token_id(conn, r["TOKEN_ID"])
                if resolved is None:
                    continue
                if market is not None and resolved.condition_id != market:
                    continue
                # Original outcome-token size: BUY → takerAmount, SELL → makerAmount.
                original = int(
                    r["TAKER_AMOUNT"] if r["SIDE"] == "BUY" else r["MAKER_AMOUNT"]
                )
                matched = original - int(r["REMAINING_AMOUNT"])
                outcome_label = resolved.market.erc1155_tokens[
                    resolved.outcome_index
                ][1]
                out.append(
                    OpenOrder(
                        id=r["ORDER_ID"],
                        owner=user.user_id,
                        maker_address=r["MAKER"],
                        market=resolved.condition_id,
                        asset_id=r["TOKEN_ID"],
                        side=r["SIDE"],
                        original_size=size_to_decimal_str(original),
                        size_matched=size_to_decimal_str(matched),
                        price=price_to_decimal_str(int(r["PRICE"])),
                        outcome=outcome_label,
                        created_at=int(r["CREATED_AT"]),
                        expiration=str(r["EXPIRATION"]),
                        order_type=r["ORDER_TYPE"],
                    )
                )
        return out

    def cancel_orders(self, user: User, order_ids: list[str]) -> CancelOrdersResponse:
        """Cancel a set of the caller's live orders by id (§8.2)."""
        order_ids = list(dict.fromkeys(order_ids))  # dedup, preserve order (Polymarket ignores dupes)
        result = CancelOrdersResponse()
        now = int(time.time())
        with self._db.write() as conn:
            for order_id in order_ids:
                cur = conn.execute(
                    "UPDATE orders SET STATUS = 'cancelled' "
                    f"WHERE ORDER_ID = %s AND API_KEY = %s AND {TableRead.LIVE_ORDER}",
                    (order_id, user.api_key, now),
                )
                if cur.rowcount > 0:
                    result.canceled.append(order_id)
                else:
                    result.not_canceled[order_id] = (
                        "order not found, not yours, or not live"
                    )
        return result

    def cancel_all(self, user: User) -> CancelOrdersResponse:
        """Cancel every live order owned by the caller."""
        with self._db.read() as conn:
            ids = [
                r["ORDER_ID"]
                for r in conn.execute(
                    "SELECT ORDER_ID FROM orders "
                    f"WHERE API_KEY = %s AND {TableRead.LIVE_ORDER}",
                    (user.api_key, int(time.time())),
                ).fetchall()
            ]
        return self.cancel_orders(user, ids)

    def cancel_market_orders(
        self, user: User, market: str | None, asset_id: str | None
    ) -> CancelOrdersResponse:
        """Cancel the caller's live orders filtered by condition_id (`market`)
        and/or token_id (`asset_id`). With neither filter, cancels all."""
        clauses = ["API_KEY = %s"]
        params: list = [user.api_key]
        if asset_id is not None:
            clauses.append("TOKEN_ID = %s")
            params.append(asset_id)
        if market is not None:
            # `market` is a condition_id; resolve it to the market's token ids.
            with self._db.read() as conn:
                m = TableRead.read_market_by_condition_id(conn, ConditionId(market))
            token_ids = [t for t, _label in m.erc1155_tokens] if m else ["\x00"]
            placeholders = ",".join("%s" for _ in token_ids)
            clauses.append(f"TOKEN_ID IN ({placeholders})")
            params.extend(token_ids)
        # Last, per the positional-parameter rule: any clause added above
        # this line without touching both lists the same way still lines up.
        clauses.append(TableRead.LIVE_ORDER)
        params.append(int(time.time()))
        with self._db.read() as conn:
            ids = [
                r["ORDER_ID"]
                for r in conn.execute(
                    f"SELECT ORDER_ID FROM orders WHERE {' AND '.join(clauses)}",
                    params,
                ).fetchall()
            ]
        return self.cancel_orders(user, ids)

    def get_book(self, token_id: str) -> OrderBookSummary:
        """Polymarket's order book for one outcome token (§8.5)."""
        with self._db.read() as conn:
            resolved = resolve_by_token_id(conn, token_id)
            if resolved is None:
                raise MarketNotFoundError(0)
            last = conn.execute(
                TableRead.TOKEN_PRINTS_CTE
                + "SELECT PRICE FROM prints ORDER BY MATCH_TIME DESC LIMIT 1",
                ([token_id], [token_id]),
            ).fetchone()
        snap = feed.book(token_id)

        def levels(side: tuple[tuple[int, int], ...]) -> list[OrderBookLevel]:
            return [
                OrderBookLevel(
                    price=price_to_decimal_str(p), size=size_to_decimal_str(s)
                )
                for p, s in side
            ]

        bid_levels = levels(snap.bids) if snap else []
        ask_levels = levels(snap.asks) if snap else []
        last_trade_price = (
            price_to_decimal_str(int(last["PRICE"])) if last is not None else "0"
        )
        timestamp = str(int(datetime.now(timezone.utc).timestamp() * 1000))
        digest_src = "".join(
            f"{l.price}:{l.size}|" for l in (*bid_levels, *ask_levels)
        )
        book_hash = hashlib.sha1(digest_src.encode()).hexdigest()  # noqa: S324
        return OrderBookSummary(
            market=resolved.condition_id,
            asset_id=token_id,
            timestamp=timestamp,
            hash=book_hash,
            bids=bid_levels,
            asks=ask_levels,
            last_trade_price=last_trade_price,
        )

    def get_books(self, token_ids: list[str]) -> list[OrderBookSummary]:
        """Batch book read (§8.5). Skips unknown token ids."""
        out: list[OrderBookSummary] = []
        for token_id in token_ids:
            try:
                out.append(self.get_book(token_id))
            except MarketNotFoundError:
                continue
        return out

    _INTERVAL_HOURS = {
        "1h": 1, "6h": 6, "1d": 24, "1w": 168, "1m": 720, "max": 24 * 365 * 100,
    }

    def get_prices_history(
        self,
        token_id: str,
        *,
        start_ts: int | None = None,
        end_ts: int | None = None,
        interval: str = "1d",
        fidelity: int = 0,
    ) -> dict:
        """Trade-price history for one outcome token (§8.6).

        Returns ``{"history": [{"t": int_seconds, "p": float_0_1}]}`` ascending.
        `interval` selects a trailing window unless explicit start/end are given;
        `fidelity` (minutes) thins the series.
        """
        now = int(datetime.now(timezone.utc).timestamp())
        end = end_ts if end_ts is not None else now
        if start_ts is not None:
            start = start_ts
        else:
            hours = self._INTERVAL_HOURS.get(interval, 24)
            start = end - hours * 3600
        with self._db.read() as conn:
            rows = conn.execute(
                TableRead.TOKEN_PRINTS_CTE
                + "SELECT MATCH_TIME, PRICE FROM prints "
                  "WHERE MATCH_TIME >= %s AND MATCH_TIME <= %s "
                  "ORDER BY MATCH_TIME ASC",
                ([token_id], [token_id], start, end),
            ).fetchall()
            # A price holds until the next trade, so a window containing no
            # trade is not a window with no price. Without this the one-day
            # series of a market whose last print is 26 hours old came back
            # empty and the card drew its no-data placeholder beside a live
            # headline -- with the tape as sparse as it is, that is most cards.
            #
            # Stamped AT `start`, not at its own time: left where it happened, a
            # month-old print would stretch a one-day chart back over a range
            # the caller never asked for.
            opening = conn.execute(
                TableRead.TOKEN_PRINTS_CTE
                + "SELECT PRICE FROM prints WHERE MATCH_TIME < %s "
                  "ORDER BY MATCH_TIME DESC LIMIT 1",
                ([token_id], [token_id], start),
            ).fetchone()
        points = [
            {"t": int(r["MATCH_TIME"]), "p": price_to_float(int(r["PRICE"]))}
            for r in rows
        ]
        if opening is not None:
            points.insert(
                0, {"t": start, "p": price_to_float(int(opening["PRICE"]))}
            )
        # Optional fidelity thinning (minutes between kept points).
        if fidelity > 0 and points:
            step = fidelity * 60
            thinned = [points[0]]
            for pt in points[1:]:
                if pt["t"] - thinned[-1]["t"] >= step:
                    thinned.append(pt)
            if thinned[-1] is not points[-1]:
                thinned.append(points[-1])
            points = thinned
        return {"history": points}

    def get_midpoint(self, token_id: str) -> dict:
        best_bid, best_ask = feed.tops([token_id]).get(token_id, (None, None))
        if best_bid is None or best_ask is None:
            raise NotFoundError("no book for token")
        return {"mid": price_to_decimal_str((best_bid + best_ask) // 2)}

    def get_price(self, token_id: str, side: str) -> dict:
        best_bid, best_ask = feed.tops([token_id]).get(token_id, (None, None))
        chosen = best_ask if side == "BUY" else best_bid
        if chosen is None:
            raise NotFoundError("no Polymarket book on that side")
        return {"price": price_to_decimal_str(chosen)}

    def get_last_trade_price(self, token_id: str) -> dict:
        with self._db.read() as conn:
            row = conn.execute(
                TableRead.TOKEN_PRINTS_CTE
                + "SELECT PRICE, SIDE FROM prints ORDER BY MATCH_TIME DESC LIMIT 1",
                ([token_id], [token_id]),
            ).fetchone()
        if row is None:
            raise NotFoundError("no trades for token")
        return {"price": price_to_decimal_str(int(row["PRICE"])), "side": row["SIDE"]}

    # --- internals ------------------------------------------------------

    def _resolve_token(self, payload: PlaceOrderRequest) -> ResolvedOutcome:
        with self._db.read() as conn:
            resolved = resolve_by_token_id(conn, payload.token_id)
        if resolved is None:
            raise MarketStateError(f"unknown token_id '{payload.token_id}'")
        return resolved

    @staticmethod
    def _fill_amounts(side: str, collateral: int, shares: int) -> tuple[str, str]:
        """(makingAmount, takingAmount) decimal strings for what the agent
        paid or received, or ("","") when nothing filled."""
        if shares <= 0:
            return "", ""
        if side == "BUY":
            return size_to_decimal_str(collateral), size_to_decimal_str(shares)
        return size_to_decimal_str(shares), size_to_decimal_str(collateral)

    def _build_replay_response(self, conn, order_id: str) -> OrderResponse:
        """Reconstruct an OrderResponse for an already-placed order (idempotent
        replay) from its trades: a FAILED one replays as success=False (spec
        §5.5). errorMsg is not reconstructed (it was never persisted)."""
        row = self._get_order_row(conn, order_id)
        trades = conn.execute(
            "SELECT TRADE_ID, TRADE_SIZE, PRICE, MATCH_KIND, TRANSACTION_HASH, STATUS "
            "FROM trades WHERE TAKER_ORDER_ID = %s ORDER BY MATCH_TIME",
            (order_id,),
        ).fetchall()
        settled = [t for t in trades if t["STATUS"] != "FAILED"]
        making_amount, taking_amount = self._fill_amounts(
            row["SIDE"],
            sum(
                (
                    int(t["PRICE"])
                    if (t["MATCH_KIND"] or "NORMAL") == "NORMAL"
                    else _PRICE_ONE - int(t["PRICE"])
                )
                * int(t["TRADE_SIZE"])
                // _PRICE_ONE
                for t in settled
            ),
            sum(int(t["TRADE_SIZE"]) for t in settled),
        )
        tx_hashes = [t["TRANSACTION_HASH"] for t in settled if t["TRANSACTION_HASH"]]
        return OrderResponse(
            success=len(settled) == len(trades),
            orderID=order_id,
            status=row["STATUS"],
            transactionsHashes=list(dict.fromkeys(tx_hashes)),
            takingAmount=taking_amount,
            makingAmount=making_amount,
            tradeIDs=[t["TRADE_ID"] for t in settled],
        )

    @staticmethod
    def _amounts_from_price_size(
        side: str, price: Decimal, size: int
    ) -> tuple[int, int]:
        """Return (makerAmount, takerAmount) given order side, price, and outcome-token size.

        BUY: maker offers collateral (USDC) to receive outcome tokens.
            makerAmount = price * size, takerAmount = size.
        SELL: maker offers outcome tokens to receive collateral.
            makerAmount = size, takerAmount = price * size.
        """
        collateral = (Decimal(price) * Decimal(size)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
        collateral_int = int(collateral)
        if side == "BUY":
            return collateral_int, int(size)
        return int(size), collateral_int

    def _check_balance(
        self, eth_address: str, side: str, maker_amount: int, token_id_int: int
    ) -> None:
        if side == "BUY":
            bal = self._onchain.usd_balance(eth_address)
            if bal < maker_amount:
                raise InsufficientBalanceError(f"need {maker_amount} apUSD, have {bal}")
        else:
            bal = self._onchain.ctf_balance(eth_address, token_id_int)
            if bal < maker_amount:
                raise InsufficientBalanceError(
                    f"need {maker_amount} outcome tokens, have {bal}"
                )

    def _insert_order(
        self,
        conn,
        *,
        api_key: str,
        order: OrderData,
        order_id: str,
        signature: bytes,
        price_int: int,
        order_type: str,
    ) -> None:
        order_json = json.dumps(self._signed_order_payload(order, signature))
        # REMAINING_AMOUNT is tracked in outcome-token units regardless of side.
        # BUY: takerAmount = outcome qty. SELL: makerAmount = outcome qty.
        outcome_remaining = order.takerAmount if order.side == 0 else order.makerAmount
        conn.execute(
            """
            INSERT INTO orders (
                API_KEY, PRICE, POST_ONLY, ORDER_TYPE,
                SALT, MAKER, TAKER, SIGNER,
                TOKEN_ID, MAKER_AMOUNT, TAKER_AMOUNT,
                EXPIRATION, NONCE, FEE_RATE_BPS,
                SIDE, SIGNATURE_TYPE, SIGNATURE, ORDER_JSON,
                STATUS, REMAINING_AMOUNT, CREATED_AT, ORDER_ID
            ) VALUES (%s, %s, 0, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                api_key,
                price_int,
                order_type,
                str(order.salt),
                order.maker,
                order.taker,
                order.signer,
                str(order.tokenId),
                order.makerAmount,
                order.takerAmount,
                order.expiration,
                order.nonce,
                order.feeRateBps,
                "BUY" if order.side == 0 else "SELL",
                "EIP712",
                "0x" + signature.hex(),
                order_json,
                "live",
                outcome_remaining,
                int(datetime.now(timezone.utc).timestamp()),
                order_id,
            ),
        )

    @staticmethod
    def _signed_order_payload(order: OrderData, signature: bytes) -> dict:
        d = asdict(order)
        d["signature"] = "0x" + signature.hex()
        # JSON can't carry the 256-bit ints; stringify the big ones
        for big in ("salt", "tokenId", "makerAmount", "takerAmount"):
            d[big] = str(d[big])
        return d

    @staticmethod
    def _order_from_json(row) -> tuple[OrderData, bytes]:
        signed = json.loads(row["ORDER_JSON"])
        order = OrderData(
            salt=int(signed["salt"]),
            maker=signed["maker"],
            signer=signed["signer"],
            taker=signed["taker"],
            tokenId=int(signed["tokenId"]),
            makerAmount=int(signed["makerAmount"]),
            takerAmount=int(signed["takerAmount"]),
            expiration=int(signed["expiration"]),
            nonce=int(signed["nonce"]),
            feeRateBps=int(signed["feeRateBps"]),
            side=int(signed["side"]),
            signatureType=int(signed["signatureType"]),
        )
        return order, bytes.fromhex(signed["signature"][2:])

    @staticmethod
    def _compute_order_id(order: OrderData) -> str:
        # Stable id derived from the signed fields. Not the EIP-712 hash; this
        # is purely an internal identifier.
        payload = json.dumps(
            {
                k: (str(v) if isinstance(v, int) else v)
                for k, v in asdict(order).items()
            },
            sort_keys=True,
        ).encode()
        return "0x" + keccak(payload).hex()

    @staticmethod
    def _price_int(order: OrderData) -> int:
        # price = collateral/asset scaled by 10^6.
        # For BUY: maker=collateral, taker=asset.
        maker = Decimal(order.makerAmount)
        taker = Decimal(order.takerAmount)
        if maker <= 0 or taker <= 0:
            raise ValueError("amounts must be positive")
        if order.side == 0:
            price = maker / taker
        else:
            price = taker / maker
        return int((price * _USDC_SCALE).to_integral_value(rounding=ROUND_HALF_UP))

    @staticmethod
    def _get_order_row(conn, order_id: str):
        row = conn.execute(
            "SELECT * FROM orders WHERE ORDER_ID = %s LIMIT 1", (order_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"order {order_id} not found post-insert")
        return row

    def _take(
        self,
        conn,
        row,
        agent_order: OrderData,
        quote: tuple[MarketRef, bool, BookReplica] | None,
    ) -> Match | None:
        house = feed.HOUSE
        if house is None or quote is None:
            return None
        ref, yes, rep = quote
        buy = row["SIDE"] == "BUY"
        limit = int(row["PRICE"])
        remaining = int(row["REMAINING_AMOUNT"])
        takes = rep.take(
            row["API_KEY"], buy == yes, limit if yes else _PRICE_ONE - limit, remaining
        )
        size = sum(t.size for t in takes)
        cost = sum((t.price if yes else _PRICE_ONE - t.price) * t.size for t in takes)
        house_amount = self._house_amounts(agent_order, size, cost)
        if not 0 < house_amount < size:
            return None
        house_order = OrderData(
            salt=secrets.randbits(256),
            maker=house.user.eth_address,
            signer=house.user.eth_address,
            taker=_ZERO_ADDR,
            tokenId=(
                int(ref.no_token if yes else ref.yes_token)
                if buy
                else agent_order.tokenId
            ),
            makerAmount=house_amount,
            takerAmount=size,
            expiration=0,
            nonce=0,
            feeRateBps=0,
            side=0,
            signatureType=0,
        )
        check_state(
            _orders_cross(
                agent_order.makerAmount,
                agent_order.takerAmount,
                row["SIDE"],
                house_amount,
                size,
            ),
            f"house order does not cross order {row['ORDER_ID']}",
        )
        house_signature = sign_order(
            house.user.eth_key, self._onchain._client.deployment, house_order
        )
        house_order_id = self._compute_order_id(house_order)
        self._insert_order(
            conn,
            api_key=house.user.api_key,
            order=house_order,
            order_id=house_order_id,
            signature=house_signature,
            price_int=self._price_int(house_order),
            order_type="FOK",
        )
        conn.execute(
            "UPDATE orders SET REMAINING_AMOUNT = 0, STATUS = 'matched' WHERE ORDER_ID = %s",
            (house_order_id,),
        )
        left = remaining - size
        conn.execute(
            "UPDATE orders SET REMAINING_AMOUNT = %s, STATUS = %s WHERE ORDER_ID = %s",
            (
                left,
                "live" if left and row["ORDER_TYPE"] != "FAK" else "matched",
                row["ORDER_ID"],
            ),
        )
        match = Match(
            takes=takes,
            size=size,
            house_amount=house_amount,
            agent_amount=size - house_amount if buy else size,
            kind="MINT" if buy else "NORMAL",
            house_order=house_order,
            house_signature=house_signature,
            trade_id=f"{row['ORDER_ID']}-{house_order_id}-{secrets.token_hex(8)}",
        )
        self._insert_trade(conn, row, self._get_order_row(conn, house_order_id), match)
        return match

    @staticmethod
    def _house_amounts(agent: OrderData, size: int, cost: int) -> int:
        if agent.side == 0:
            p = _exchange_price(agent.makerAmount, agent.takerAmount, "BUY")
            return max(
                size - cost // _PRICE_ONE,
                -(-(_EXCHANGE_ONE - p) * size // _EXCHANGE_ONE),
                size - size * agent.makerAmount // agent.takerAmount,
            )
        p = _exchange_price(agent.makerAmount, agent.takerAmount, "SELL")
        return max(
            -(-cost // _PRICE_ONE),
            -(-p * size // _EXCHANGE_ONE),
            size * agent.takerAmount // agent.makerAmount,
        )

    @staticmethod
    def _insert_trade(conn, taker_row, maker_row, match: Match) -> None:
        token_id = taker_row["TOKEN_ID"]
        resolved = resolve_by_token_id(conn, token_id)
        condition_id = resolved.condition_id if resolved else token_id
        outcome_label = (
            resolved.market.erc1155_tokens[resolved.outcome_index][1]
            if resolved else ""
        )
        maker_user_id = TableRead.get_user_id_by_api_key(conn, maker_row["API_KEY"])
        maker_side = maker_row["SIDE"]
        # The maker's order is booked against ITS token, which for a
        # MINT is the complement of the taker's.
        maker_asset_id = maker_row["TOKEN_ID"]
        maker_orders_payload = [
            {
                "order_id": maker_row["ORDER_ID"],
                "owner": maker_user_id or "",         # non-secret USER_ID (§13)
                "maker_address": maker_row["MAKER"],  # eth address
                "matched_amount": str(match.size),
                "price": int(maker_row["PRICE"]),
                "fee_rate_bps": int(maker_row["FEE_RATE_BPS"]),
                "asset_id": maker_asset_id,
                "outcome": outcome_label,
                "side": maker_side,
            }
        ]
        conn.execute(
            """
            INSERT INTO trades (
                TRADE_ID, TAKER_ORDER_ID, MAKER_ORDERS, MARKET, ASSET_ID,
                MAKER_ASSET_ID, MATCH_KIND,
                PRICE, TRADE_SIZE, REMAINING_SIZE, SIDE, STATUS,
                MATCH_TIME, TRANSACTION_HASH, BUCKET_INDEX, FEE_RATE_BPS,
                TAKER_API_KEY, MAKER_API_KEY
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                match.trade_id,
                taker_row["ORDER_ID"],
                json.dumps(maker_orders_payload),
                condition_id,                 # MARKET = condition_id (§7 fix)
                token_id,                     # ASSET_ID = token_id
                maker_asset_id,
                match.kind,
                int(maker_row["PRICE"]),
                match.size,
                taker_row["REMAINING_AMOUNT"],
                taker_row["SIDE"],
                "PENDING",
                int(datetime.now(timezone.utc).timestamp()),
                None,
                0,
                int(taker_row["FEE_RATE_BPS"]),
                taker_row["API_KEY"],          # internal filter key (never serialized)
                maker_row["API_KEY"],          # internal filter key
            ),
        )

    # --- on-chain settlement -------------------------------------------

    def _settle(
        self,
        agent_order: OrderData,
        agent_signature: bytes,
        match: Match,
        *,
        wait: bool,
    ) -> tuple[str, str | None]:
        sender = self._onchain._client.admin_sender  # noqa: SLF001
        exchange = self._onchain._contracts.exchange  # noqa: SLF001
        tx_hash: str | None = None
        try:
            call = exchange.functions.matchOrders(
                self._to_solidity_order(agent_order, agent_signature),
                [self._to_solidity_order(match.house_order, match.house_signature)],
                match.agent_amount,
                [match.house_amount],
            )
            try:
                tx = sender.submit(call)
            except TxUnknown as unknown:
                tx = unknown.pending
            tx_hash = "0x" + tx.tx_hash.hex()
            with self._db.write() as conn:
                TableWrite.settle_trades(conn, [match.trade_id], "PENDING", tx_hash)
            if not wait:
                return "", tx_hash
            receipt = sender.wait(tx, timeout=_SETTLE_TIMEOUT_S)
        except Exception as exc:
            lost = tx_hash is None or isinstance(exc, TxDropped)
            status = "FAILED" if lost else "PENDING"
            detail = str(exc)
        else:
            status = "CONFIRMED" if receipt["status"] == 1 else "FAILED"
            detail = f"{tx_hash} reverted"
        with self._db.write() as conn:
            TableWrite.settle_trades(conn, [match.trade_id], status, tx_hash)
        if status != "CONFIRMED":
            log.warning("matchOrders for %s is %s: %s", match.trade_id, status, detail)
        return (f"settlement failed: {detail}" if status == "FAILED" else ""), tx_hash

    def settle_pending(self, now: int) -> None:
        with self._db.read() as conn:
            pending = TableRead.pending_settlements(conn, now - _RECEIPT_CHECK_AFTER_S)
            unsent = TableRead.unsent_settlements(conn, now - FORGET_DONE_AFTER_S)
        exchange = self._onchain._contracts.exchange  # noqa: SLF001
        for row in unsent:
            house = self._to_solidity_order(*self._order_from_json(row))
            filled, _ = exchange.functions.orderStatus(
                exchange.functions.hashOrder(house).call()
            ).call()
            status = "CONFIRMED" if filled else "FAILED"
            log.info("matchOrders for %s left no hash; it is %s", row["TRADE_ID"], status)
            with self._db.write() as conn:
                TableWrite.settle_trades(conn, [row["TRADE_ID"]], status, None)
        if not pending:
            return
        rpc = Web3ChainRpc(self._onchain._client.web3)  # noqa: SLF001
        receipts = rpc.receipts([bytes.fromhex(h[2:]) for h, _, _ in pending])
        with self._db.write() as conn:
            for (tx_hash, matched_at, trade_ids), receipt in zip(pending, receipts):
                if receipt is None and matched_at >= now - FORGET_DONE_AFTER_S:
                    continue
                landed = receipt is not None and receipt["status"] == 1
                status = "CONFIRMED" if landed else "FAILED"
                log.info("matchOrders %s for %s is %s", tx_hash, trade_ids, status)
                TableWrite.settle_trades(conn, trade_ids, status, tx_hash)

    @staticmethod
    def _to_solidity_order(order: OrderData, signature: bytes) -> tuple:
        return (
            order.salt,
            Web3.to_checksum_address(order.maker),
            Web3.to_checksum_address(order.signer),
            Web3.to_checksum_address(order.taker),
            order.tokenId,
            order.makerAmount,
            order.takerAmount,
            order.expiration,
            order.nonce,
            order.feeRateBps,
            order.side,
            order.signatureType,
            signature,
        )
