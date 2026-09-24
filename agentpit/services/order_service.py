import hashlib
import json
import logging
import secrets
import time

import psycopg
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from eth_utils.crypto import keccak
from web3 import Web3

from agentpit.datastructures.cancel_orders_response import CancelOrdersResponse
from agentpit.datastructures.orderbook_summary import OrderBookLevel, OrderBookSummary
from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.order_response import OrderResponse
from agentpit.datastructures.place_order_request import PlaceOrderRequest
from agentpit.datastructures.user import User
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import (
    BusinessRuleError,
    InsufficientBalanceError,
    MarketNotFoundError,
    MarketStateError,
    NotFoundError,
    OrderNotFilledError,
)
from agentpit.onchain.admin import OnchainAdmin
from agentpit.onchain.order_signer import OrderData, sign_order
from agentpit.onchain.user_wallet import send_admin_tx
from agentpit.datastructures.open_order import OpenOrder
from agentpit.polymarket.format import decimal_str_to_size_micro, price_to_decimal_str, price_to_float, size_to_decimal_str
from agentpit.polymarket.resolve import resolve_by_token_id

log = logging.getLogger(__name__)

_USDC_DECIMALS = 6
_USDC_SCALE = Decimal(10**_USDC_DECIMALS)
_PRICE_ONE = 10**_USDC_DECIMALS  # stored PRICE units that equal $1.00
_ZERO_ADDR = "0x0000000000000000000000000000000000000000"

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
    taker_maker: int, taker_taker: int, taker_side: str,
    maker_maker: int, maker_taker: int, maker_side: str,
) -> bool:
    """CalculatorHelper.isCrossing — exact replica over both orders' amounts."""
    if taker_taker == 0 or maker_taker == 0:
        return True
    pa = _exchange_price(taker_maker, taker_taker, taker_side)
    pb = _exchange_price(maker_maker, maker_taker, maker_side)
    if taker_side == "BUY":
        if maker_side == "BUY":
            return pa + pb >= _EXCHANGE_ONE   # both bids → MINT
        return pa >= pb                       # taker bid vs maker ask
    if maker_side == "BUY":
        return pb >= pa                       # taker ask vs maker bid
    return pa + pb <= _EXCHANGE_ONE           # both asks → MERGE


class OrderService:
    """Place + match + settle the simple-trade flow.

    The orderbook is off-chain (the `orders` table). When two opposing orders
    cross, the operator (admin key) submits a `matchOrders` tx so settlement
    happens on-chain via the deployed CTFExchange.
    """

    def __init__(self, db: DbSession, onchain: OnchainAdmin):
        self._db = db
        self._onchain = onchain

    # --- public API -----------------------------------------------------

    def place_order(
        self,
        user: User,
        payload: PlaceOrderRequest,
        *,
        balance_hint: int | None = None,
    ) -> OrderResponse:
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
                if existing is not None and self._safe_row(existing) is not None:
                    return self._build_replay_response(conn, existing)
            if existing is not None:
                # Stale claim: its order row was purged (cancelled + cleaned up)
                # before this retry arrived. Replaying a purged, never-filled
                # order helps nobody — drop the claim, place fresh.
                with self._db.write() as conn:
                    TableWrite.delete_idempotency_key(
                        conn, api_key=user.api_key, client_order_id=coid
                    )
        token_id_int, _token_id_str = self._resolve_token(payload)
        size_micro = decimal_str_to_size_micro(str(payload.size))
        maker_amount, taker_amount = self._amounts_from_price_size(
            payload.side, payload.price, size_micro
        )

        # Pre-flight balance check — reject obvious losers before signing.
        # `balance_hint` lets a batch caller (the mirror) supply the relevant
        # balance it already read this cycle, so we skip the on-chain read —
        # the dominant per-order cost when replicating a deep book.
        self._check_balance(
            user.eth_address, payload.side, maker_amount, token_id_int,
            balance_hint=balance_hint,
        )

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
            with self._db.write() as conn:
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
                taker_row = self._get_order_row(conn, order_id)
                matches = self._match(conn, taker_row)
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

        tx_hashes: list[str] = []
        if matches:
            try:
                hashes = self._settle_on_chain(order, signature, matches)
                tx_hashes = ["0x" + h.hex() for h in hashes]
            except Exception as exc:
                log.exception("on-chain settlement failed for order %s", order_id)
                with self._db.write() as conn:
                    conn.execute(
                        "UPDATE trades SET STATUS = 'FAILED' "
                        "WHERE TAKER_ORDER_ID = %s",
                        (order_id,),
                    )
                failed_row = self._safe_row(order_id)
                return OrderResponse(
                    success=False,
                    orderID=order_id,
                    status=failed_row["STATUS"] if failed_row else "live",
                    errorMsg=f"settlement failed: {exc}",
                )

        with self._db.read() as conn:
            row = self._get_order_row(conn, order_id)
        # takingAmount/makingAmount come from the immediate match (taker's
        # perspective), in decimal strings (§4); "" when nothing filled.
        # The taker transacts at its OWN limit price for every fill (see
        # _taker_fill_amount) — true for NORMAL fills and for MINT/MERGE,
        # where taker and maker pay different prices summing to 1 — so the
        # taker's collateral is taker_price × filled, not the maker's price.
        filled_micro = sum(int(m["trade_size"]) for m in matches)
        making_amount, taking_amount = self._fill_amounts(
            payload.side, price_int, filled_micro
        )

        return OrderResponse(
            success=True,
            orderID=order_id,
            status=row["STATUS"],
            transactionsHashes=tx_hashes,
            takingAmount=taking_amount,
            makingAmount=making_amount,
            tradeIDs=[m["trade_id"] for m in matches],
        )

    def _prepare_resting_orders(
        self,
        user: User,
        payloads: "list[PlaceOrderRequest]",
        balance_hints: "list[int | None] | None",
    ) -> "list[tuple[OrderData, str, bytes, int, str]]":
        """Sign + validate a batch of resting orders OUTSIDE any transaction
        (CPU only). Returns (order, order_id, signature, price_int, order_type)
        for the orders that pass; unknown-token / underfunded payloads drop."""
        if not payloads:
            return []
        hints = (
            balance_hints if balance_hints is not None else [None] * len(payloads)
        )
        # Resolve each distinct token id once (one read), not per order.
        distinct = {p.token_id for p in payloads}
        with self._db.read() as conn:
            resolved = {t: resolve_by_token_id(conn, t) for t in distinct}
        prepared: "list[tuple[OrderData, str, bytes, int, str]]" = []
        for payload, hint in zip(payloads, hints):
            r = resolved.get(payload.token_id)
            if r is None:
                continue  # unknown token
            token_id_int = int(r.token_id)
            size_micro = decimal_str_to_size_micro(str(payload.size))
            maker_amount, taker_amount = self._amounts_from_price_size(
                payload.side, payload.price, size_micro
            )
            try:
                self._check_balance(
                    user.eth_address, payload.side, maker_amount, token_id_int,
                    balance_hint=hint,
                )
            except InsufficientBalanceError:
                continue  # skip underfunded; not fatal for a batch
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
            signature = sign_order(
                user.eth_key, self._onchain._client.deployment, order
            )
            prepared.append((
                order, self._compute_order_id(order), signature,
                self._price_int(order), payload.order_type,
            ))
        return prepared

    def place_resting_orders(
        self,
        user: User,
        payloads: "list[PlaceOrderRequest]",
        *,
        balance_hints: "list[int | None] | None" = None,
    ) -> "list[str]":
        """Insert many NON-CROSSING resting orders in a single transaction.

        For a trusted batch caller (the liquidity mirror) whose orders are
        already classified as non-crossing: a resting non-crossing order matches
        nothing, so we skip the per-order match/settle — one signing pass + one
        bulk INSERT instead of ~3 DB round-trips per order. Returns the inserted
        order_ids; unknown-token / underfunded payloads are omitted.

        NOT for crossing/taker orders — those must go through place_order so
        they match and settle on-chain.
        """
        return self.replace_resting_orders(
            user, [], payloads, balance_hints=balance_hints
        )

    def replace_resting_orders(
        self,
        user: User,
        cancel_ids: "list[str]",
        payloads: "list[PlaceOrderRequest]",
        *,
        balance_hints: "list[int | None] | None" = None,
    ) -> "list[str]":
        """Atomically cancel `cancel_ids` and insert non-crossing `payloads` in
        ONE transaction, so a concurrent /book read never sees the intermediate
        empty state — the cancel-then-replace gap that makes the book flicker
        between full and empty during fast re-quoting.

        Cancels apply before inserts within the transaction (spec §5); the
        orders are non-crossing (caller-classified), so no matching/settlement
        runs. Returns the inserted order_ids.
        """
        prepared = self._prepare_resting_orders(user, payloads, balance_hints)
        if not cancel_ids and not prepared:
            return []
        now = int(time.time())
        with self._db.write() as conn:
            for oid in dict.fromkeys(cancel_ids):
                conn.execute(
                    "UPDATE orders SET STATUS = 'cancelled' "
                    f"WHERE ORDER_ID = %s AND API_KEY = %s AND {TableRead.LIVE_ORDER}",
                    (oid, user.api_key, now),
                )
            for order, order_id, signature, price_int, order_type in prepared:
                self._insert_order(
                    conn, api_key=user.api_key, order=order, order_id=order_id,
                    signature=signature, price_int=price_int, order_type=order_type,
                )
        return [order_id for _o, order_id, _s, _p, _t in prepared]

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
        """Aggregated order book for one outcome token (§8.5)."""
        with self._db.read() as conn:
            resolved = resolve_by_token_id(conn, token_id)
            if resolved is None:
                raise MarketNotFoundError(0)
            rows = conn.execute(
                "SELECT SIDE, PRICE, SUM(REMAINING_AMOUNT) AS SZ FROM orders "
                f"WHERE TOKEN_ID = %s AND {TableRead.LIVE_ORDER} GROUP BY SIDE, PRICE",
                (token_id, int(time.time())),
            ).fetchall()
            last = conn.execute(
                TableRead.TOKEN_PRINTS_CTE
                + "SELECT PRICE FROM prints ORDER BY MATCH_TIME DESC LIMIT 1",
                ([token_id], [token_id]),
            ).fetchone()
        bids = sorted(
            (r for r in rows if r["SIDE"] == "BUY"),
            key=lambda r: -int(r["PRICE"]),
        )
        asks = sorted(
            (r for r in rows if r["SIDE"] == "SELL"),
            key=lambda r: int(r["PRICE"]),
        )

        def level(r) -> OrderBookLevel:
            return OrderBookLevel(
                price=price_to_decimal_str(int(r["PRICE"])),
                size=size_to_decimal_str(int(r["SZ"])),
            )

        bid_levels = [level(r) for r in bids]
        ask_levels = [level(r) for r in asks]
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

    def _best_bid_ask(self, token_id: str) -> tuple[int | None, int | None]:
        """(best_bid_price_int, best_ask_price_int) from the live book."""
        with self._db.read() as conn:
            rows = conn.execute(
                "SELECT SIDE, PRICE FROM orders "
                f"WHERE TOKEN_ID = %s AND {TableRead.LIVE_ORDER}",
                (token_id, int(time.time())),
            ).fetchall()
        bids = [int(r["PRICE"]) for r in rows if r["SIDE"] == "BUY"]
        asks = [int(r["PRICE"]) for r in rows if r["SIDE"] == "SELL"]
        return (max(bids) if bids else None, min(asks) if asks else None)

    def get_midpoint(self, token_id: str) -> dict:
        best_bid, best_ask = self._best_bid_ask(token_id)
        if best_bid is None or best_ask is None:
            raise NotFoundError("no book for token")
        return {"mid": price_to_decimal_str((best_bid + best_ask) // 2)}

    def get_price(self, token_id: str, side: str) -> dict:
        best_bid, best_ask = self._best_bid_ask(token_id)
        chosen = best_ask if side == "BUY" else best_bid
        if chosen is None:
            raise NotFoundError("no resting orders on that side")
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

    def _resolve_token(self, payload: PlaceOrderRequest) -> tuple[int, str]:
        """Resolve the order's canonical token_id to (token_id_int, token_id_str)."""
        with self._db.read() as conn:
            resolved = resolve_by_token_id(conn, payload.token_id)
        if resolved is None:
            raise MarketStateError(f"unknown token_id '{payload.token_id}'")
        return int(resolved.token_id), resolved.token_id

    def _safe_row(self, order_id: str):
        with self._db.read() as conn:
            try:
                return self._get_order_row(conn, order_id)
            except RuntimeError:
                return None

    @staticmethod
    def _fill_amounts(side: str, price_int: int, filled_micro: int) -> tuple[str, str]:
        """(makingAmount, takingAmount) decimal strings for a taker's fills, or
        ("","") when nothing filled. The taker transacts at its OWN limit price
        for every fill, so collateral is taker_price x filled."""
        if filled_micro <= 0:
            return "", ""
        collateral_micro = (price_int * filled_micro) // _PRICE_ONE
        if side == "BUY":
            return (
                size_to_decimal_str(collateral_micro),  # USDC given
                size_to_decimal_str(filled_micro),       # shares received
            )
        return (
            size_to_decimal_str(filled_micro),           # shares given
            size_to_decimal_str(collateral_micro),       # USDC received
        )

    def _build_replay_response(self, conn, order_id: str) -> OrderResponse:
        """Reconstruct an OrderResponse for an already-placed order (idempotent
        replay). Fill amounts + trade ids come from the order's confirmed trades;
        transaction hashes are best-effort (the normal path returns them from the
        in-memory settlement, so DB rows may not carry them)."""
        row = self._get_order_row(conn, order_id)
        trades = conn.execute(
            "SELECT TRADE_ID, TRADE_SIZE, TRANSACTION_HASH, STATUS FROM trades "
            "WHERE TAKER_ORDER_ID = %s ORDER BY MATCH_TIME",
            (order_id,),
        ).fetchall()
        # Settlement is all-or-nothing per taker, so any FAILED trade means the
        # original attempt failed -> replay that failure (spec §5.5). errorMsg
        # is not reconstructed (the exception text was never persisted).
        has_failed = any(t["STATUS"] == "FAILED" for t in trades)
        confirmed = [t for t in trades if t["STATUS"] != "FAILED"]
        filled_micro = sum(int(t["TRADE_SIZE"]) for t in confirmed)
        making_amount, taking_amount = self._fill_amounts(
            row["SIDE"], int(row["PRICE"]), filled_micro
        )
        tx_hashes = [t["TRANSACTION_HASH"] for t in confirmed if t["TRANSACTION_HASH"]]
        return OrderResponse(
            success=not has_failed,
            orderID=order_id,
            status=row["STATUS"],
            transactionsHashes=tx_hashes,
            takingAmount=taking_amount,
            makingAmount=making_amount,
            tradeIDs=[t["TRADE_ID"] for t in confirmed],
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
        self,
        eth_address: str,
        side: str,
        maker_amount: int,
        token_id_int: int,
        *,
        balance_hint: int | None = None,
    ) -> None:
        # `balance_hint`, when given, is a same-cycle cached balance for this
        # side/token; used instead of an on-chain read (the per-order read is
        # the bottleneck when the mirror replicates a deep book — resting
        # orders never move the balance, so one read per cycle is exact).
        if side == "BUY":
            bal = (
                balance_hint
                if balance_hint is not None
                else self._onchain.usd_balance(eth_address)
            )
            if bal < maker_amount:
                raise InsufficientBalanceError(f"need {maker_amount} apUSD, have {bal}")
        else:
            bal = (
                balance_hint
                if balance_hint is not None
                else self._onchain.ctf_balance(eth_address, token_id_int)
            )
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
        # REMAINING_AMOUNT is tracked in outcome-token units regardless of side
        # so the matching loop can compare BUY and SELL orders directly.
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
    def _complement_token_id(conn, token_id: str) -> str | None:
        """Look up the binary-market complement of `token_id`, if one exists.

        Returns None when no two-outcome market contains this token (so the
        MINT/MERGE paths simply don't apply).
        """
        row = conn.execute(
            "SELECT ERC1155_TOKENS FROM markets WHERE ERC1155_TOKENS LIKE %s LIMIT 1",
            (f'%"{token_id}"%',),
        ).fetchone()
        if row is None:
            return None
        pairs = json.loads(row["ERC1155_TOKENS"])
        if len(pairs) != 2:
            return None
        a, b = pairs[0][0], pairs[1][0]
        if a == token_id:
            return b
        if b == token_id:
            return a
        return None

    @staticmethod
    def _get_order_row(conn, order_id: str):
        row = conn.execute(
            "SELECT * FROM orders WHERE ORDER_ID = %s LIMIT 1", (order_id,)
        ).fetchone()
        if row is None:
            raise RuntimeError(f"order {order_id} not found post-insert")
        return row

    def _match(self, conn, taker_row) -> list[dict]:
        """Match the taker order against resting orders.

        Considers two cross types:
        - NORMAL: same token, opposite side (existing book sweep).
        - MINT/MERGE: complementary token, same side, when prices satisfy
          the split/merge invariant ``p_taker + p_maker >= 1`` for MINT or
          ``<= 1`` for MERGE.

        Returns a list of match dicts with keys: maker_order_id, price,
        trade_size, maker_row, match_kind. Updates DB rows for both sides
        and inserts trade rows.
        """
        taker_side = taker_row["SIDE"]
        taker_price = int(taker_row["PRICE"])
        token_id = taker_row["TOKEN_ID"]
        taker_remaining = int(taker_row["REMAINING_AMOUNT"])
        # Bound once and reused across all four queries below, so a single
        # matching pass cannot disagree with itself about what time it is.
        now = int(time.time())

        opposite = "SELL" if taker_side == "BUY" else "BUY"
        if taker_side == "BUY":
            sql = (
                "SELECT * FROM orders WHERE SIDE=%s AND PRICE <= %s "
                f"AND TOKEN_ID=%s AND ORDER_ID != %s AND {TableRead.LIVE_ORDER}"
            )
        else:
            sql = (
                "SELECT * FROM orders WHERE SIDE=%s AND PRICE >= %s "
                f"AND TOKEN_ID=%s AND ORDER_ID != %s AND {TableRead.LIVE_ORDER}"
            )
        same_token = conn.execute(
            sql, (opposite, taker_price, token_id, taker_row["ORDER_ID"], now)
        ).fetchall()
        same_token = sorted(
            same_token,
            key=lambda r: (
                (int(r["PRICE"]), int(r["CREATED_AT"]))
                if taker_side == "BUY"
                else (-int(r["PRICE"]), int(r["CREATED_AT"]))
            ),
        )
        tagged: list[tuple[str, Any]] = [("NORMAL", c) for c in same_token]

        complement_id = self._complement_token_id(conn, token_id)
        if complement_id is not None:
            threshold = _PRICE_ONE - taker_price
            if taker_side == "BUY":
                comp_sql = (
                    "SELECT * FROM orders WHERE SIDE='BUY' AND PRICE >= %s "
                    f"AND TOKEN_ID=%s AND ORDER_ID != %s AND {TableRead.LIVE_ORDER}"
                )
                kind = "MINT"
                # best maker = highest price (covers more of the mint cost).
                comp_key = lambda r: (-int(r["PRICE"]), int(r["CREATED_AT"]))
            else:
                comp_sql = (
                    "SELECT * FROM orders WHERE SIDE='SELL' AND PRICE <= %s "
                    f"AND TOKEN_ID=%s AND ORDER_ID != %s AND {TableRead.LIVE_ORDER}"
                )
                kind = "MERGE"
                # best maker = lowest ask (smallest cut of the merge proceeds).
                comp_key = lambda r: (int(r["PRICE"]), int(r["CREATED_AT"]))
            comp_rows = conn.execute(
                comp_sql, (threshold, complement_id, taker_row["ORDER_ID"], now)
            ).fetchall()
            tagged.extend((kind, r) for r in sorted(comp_rows, key=comp_key))

        matches: list[dict] = []
        for kind, maker in tagged:
            if taker_remaining <= 0:
                break
            maker_remaining = int(maker["REMAINING_AMOUNT"])
            if maker_remaining <= 0:
                continue
            # The SQL pre-filter used the rounded stored PRICE; confirm the pair
            # crosses on the EXACT amounts (what the exchange checks), so a match
            # at the rounding boundary can't revert the on-chain matchOrders.
            if not _orders_cross(
                int(taker_row["MAKER_AMOUNT"]), int(taker_row["TAKER_AMOUNT"]),
                taker_side,
                int(maker["MAKER_AMOUNT"]), int(maker["TAKER_AMOUNT"]),
                maker["SIDE"],
            ):
                continue
            trade_size = min(maker_remaining, taker_remaining)
            taker_remaining -= trade_size
            new_maker_remaining = maker_remaining - trade_size
            matches.append(
                {
                    "maker_row": maker,
                    "maker_order_id": maker["ORDER_ID"],
                    "price": int(maker["PRICE"]),
                    "trade_size": trade_size,
                    "new_maker_remaining": new_maker_remaining,
                    "match_kind": kind,
                }
            )

        order_type = taker_row["ORDER_TYPE"]
        if order_type == "FOK" and taker_remaining:
            raise OrderNotFilledError(
                "order couldn't be fully filled. FOK orders are fully filled or killed."
            )
        if order_type == "FAK" and not matches:
            raise OrderNotFilledError(
                "no orders found to match with FAK order. FAK orders are partially "
                "filled or killed if no match is found."
            )

        # Apply DB updates
        for m in matches:
            new_maker_remaining = m["new_maker_remaining"]
            new_status = "matched" if new_maker_remaining == 0 else "live"
            conn.execute(
                "UPDATE orders SET REMAINING_AMOUNT=%s, STATUS=%s WHERE ORDER_ID=%s",
                (new_maker_remaining, new_status, m["maker_order_id"]),
            )
            m["trade_id"] = self._insert_trade(conn, taker_row, m)

        new_taker_status = "live" if taker_remaining and order_type != "FAK" else "matched"
        conn.execute(
            "UPDATE orders SET REMAINING_AMOUNT=%s, STATUS=%s WHERE ORDER_ID=%s",
            (taker_remaining, new_taker_status, taker_row["ORDER_ID"]),
        )
        return matches

    @staticmethod
    def _insert_trade(
        conn, taker_row, match: dict
    ) -> str:
        trade_id = "{}-{}-{}".format(
            taker_row["ORDER_ID"], match["maker_order_id"], secrets.token_hex(8)
        )
        token_id = taker_row["TOKEN_ID"]
        resolved = resolve_by_token_id(conn, token_id)
        condition_id = resolved.condition_id if resolved else token_id
        outcome_label = (
            resolved.market.erc1155_tokens[resolved.outcome_index][1]
            if resolved else ""
        )
        maker_row = match["maker_row"]
        maker_user_id = TableRead.get_user_id_by_api_key(conn, maker_row["API_KEY"])
        maker_side = maker_row["SIDE"]
        # The maker's order is booked against ITS token, which for a
        # MINT/MERGE is the complement of the taker's. Reading it from the
        # maker row is what makes the leg reconstructable; copying `token_id`
        # here is the bug this replaces.
        maker_asset_id = maker_row["TOKEN_ID"]
        match_kind = match.get("match_kind", "NORMAL")
        maker_orders_payload = [
            {
                "order_id": match["maker_order_id"],
                "owner": maker_user_id or "",         # non-secret USER_ID (§13)
                "maker_address": maker_row["MAKER"],  # eth address
                "matched_amount": str(match["trade_size"]),
                "price": int(match["price"]),
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
                trade_id,
                taker_row["ORDER_ID"],
                json.dumps(maker_orders_payload),
                condition_id,                 # MARKET = condition_id (§7 fix)
                token_id,                     # ASSET_ID = token_id
                maker_asset_id,
                match_kind,
                match["price"],
                match["trade_size"],
                taker_row["REMAINING_AMOUNT"],
                taker_row["SIDE"],
                "PENDING",
                int(datetime.now(timezone.utc).timestamp()),
                "",
                0,
                int(taker_row["FEE_RATE_BPS"]),
                taker_row["API_KEY"],          # internal filter key (never serialized)
                maker_row["API_KEY"],          # internal filter key
            ),
        )
        return trade_id

    # --- on-chain settlement -------------------------------------------

    def _settle_on_chain(
        self, taker_order: OrderData, taker_signature: bytes, matches: list[dict]
    ) -> list[bytes]:
        """Submit `matchOrders` as the operator, one tx per match-kind group.

        Each call to CTFExchange.matchOrders resolves to a single MatchType
        derived from the taker/maker token pairing, so NORMAL fills cannot
        share a tx with MINT/MERGE fills. Returns one tx hash per group.
        """
        client = self._onchain._client  # noqa: SLF001
        exchange = self._onchain._contracts.exchange  # noqa: SLF001

        groups: dict[str, list[dict]] = {}
        for m in matches:
            groups.setdefault(m.get("match_kind", "NORMAL"), []).append(m)

        taker_solidity = self._to_solidity_order(taker_order, taker_signature)
        tx_hashes: list[bytes] = []
        for group in groups.values():
            maker_solidity_orders = []
            maker_fill_amounts = []
            taker_fill_amount = 0
            for m in group:
                maker_row = m["maker_row"]
                maker_signed = json.loads(maker_row["ORDER_JSON"])
                maker_order = OrderData(
                    salt=int(maker_signed["salt"]),
                    maker=maker_signed["maker"],
                    signer=maker_signed["signer"],
                    taker=maker_signed["taker"],
                    tokenId=int(maker_signed["tokenId"]),
                    makerAmount=int(maker_signed["makerAmount"]),
                    takerAmount=int(maker_signed["takerAmount"]),
                    expiration=int(maker_signed["expiration"]),
                    nonce=int(maker_signed["nonce"]),
                    feeRateBps=int(maker_signed["feeRateBps"]),
                    side=int(maker_signed["side"]),
                    signatureType=int(maker_signed["signatureType"]),
                )
                maker_sig = bytes.fromhex(maker_signed["signature"][2:])
                maker_solidity_orders.append(
                    self._to_solidity_order(maker_order, maker_sig)
                )
                # matchOrders expects fill amounts in each side's makerAsset units.
                maker_fill_amounts.append(
                    self._maker_fill_amount(maker_order, m["trade_size"])
                )
                taker_fill_amount += self._taker_fill_amount(
                    taker_order, m["trade_size"]
                )

            fn = exchange.functions.matchOrders(
                taker_solidity,
                maker_solidity_orders,
                taker_fill_amount,
                maker_fill_amounts,
            )
            receipt = send_admin_tx(client, fn, timeout=60)
            tx_hashes.append(receipt["transactionHash"])
        return tx_hashes

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

    @staticmethod
    def _maker_fill_amount(maker: OrderData, trade_size: int) -> int:
        # `trade_size` is denominated in outcome-token units regardless of side.
        # For matchOrders, makerFillAmounts must be in the maker-asset units of
        # each maker order. SELL maker: makerAsset = outcome tokens → trade_size.
        # BUY maker: makerAsset = collateral → trade_size * price (rounded down).
        if maker.side == 1:  # SELL
            return trade_size
        # BUY maker
        ratio = Decimal(maker.makerAmount) / Decimal(maker.takerAmount)
        return int(
            (Decimal(trade_size) * ratio).to_integral_value(rounding=ROUND_HALF_UP)
        )

    @staticmethod
    def _taker_fill_amount(taker: OrderData, trade_size: int) -> int:
        # takerFillAmount is in the taker's makerAsset units.
        if taker.side == 0:  # BUY taker → makerAsset = collateral
            ratio = Decimal(taker.makerAmount) / Decimal(taker.takerAmount)
            return int(
                (Decimal(trade_size) * ratio).to_integral_value(rounding=ROUND_HALF_UP)
            )
        # SELL taker → makerAsset = outcome tokens
        return trade_size
