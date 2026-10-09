# agentpit/liquidity/mirror.py
"""MirrorEngine: glue between the WSS feed, the house, and the tape.

Three lifespan tasks (siblings of polymarket_sync / snapshot):
  run_feed:  keeps persistent WSS connections subscribed to the targets.
  run_house: refreshes the target market set and fills resting agent orders
             that Polymarket's book reaches.
  run_tape:  writes every queued print once a second.
Blocking work (DB/chain) runs via asyncio.to_thread.
"""
import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

import httpx

from agentpit.config import Settings
from agentpit.datastructures.market_state import MarketState
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.liquidity import feed, tape
from agentpit.liquidity.feed import MarketRef, MirrorState
from agentpit.liquidity.replica import MICRO, to_micro
from agentpit.polymarket.polymarket_sync import clob_market
from agentpit.services.market_service import transition
from agentpit.services.order_service import OrderService

log = logging.getLogger(__name__)


def _ref_of(m) -> "MarketRef | None":
    """Build a MarketRef from a Market, or None if it can't be mirrored (no
    upstream token or condition, or not binary)."""
    if (
        m is None
        or not m.polymarket_yes_token_id
        or not m.polymarket_condition_id
        or len(m.erc1155_tokens) < 2
    ):
        return None
    return MarketRef(
        market_id=m.market_id,
        condition_id=m.condition_id.value,
        yes_token=m.erc1155_tokens[0][0],
        no_token=m.erc1155_tokens[1][0],
        pm_yes_token=m.polymarket_yes_token_id,
        pm_condition=m.polymarket_condition_id,
    )


def _load_refs(
    db: DbSession,
    excluded_categories: "list[str] | None" = None,
    excluded_tags: "list[str] | None" = None,
) -> list[MarketRef]:
    with db.read() as conn:
        markets = TableRead.list_active_synced_markets(
            conn,
            excluded_categories=excluded_categories,
            excluded_tags=excluded_tags,
        )
    return [r for r in (_ref_of(m) for m in markets) if r is not None]


FEED_SYNC_SECONDS = 1.0
SWEEP_SECONDS = 1.0
CLOSURE_RECHECK_SECONDS = 5.0
GAME_START_WINDOW_SECONDS = 600


async def _every(name: str, seconds: float, step: Callable[[], Awaitable[None]]) -> None:
    while True:
        try:
            await step()
        except Exception:
            log.exception("house %s failed", name)
        await asyncio.sleep(seconds)


class MirrorEngine:
    def __init__(self, db: DbSession, settings: Settings, orders: OrderService):
        self._db = db
        self._cfg = settings
        self._orders = orders
        self.state = MirrorState([])
        # What the feed last actually subscribed, and when. The feed stopped
        # rebuilding on 2026-09-02 and nothing could tell: these make the gap
        # between the target set and the live subscription observable.
        self._clock = time.monotonic
        self._subscribed: frozenset[str] = frozenset()
        self._subscribed_at: "float | None" = None
        # asset -> when it was first seen as a target while unsubscribed. Each
        # missing asset is timed on its own clock, so a steady stream of new
        # markets that the feed does pick up never adds up to "stale".
        self._first_seen: "dict[str, float]" = {}
        self._http = httpx.Client(timeout=10.0)
        self._checking: set[str] = set()

    # ---- feed side -------------------------------------------------------

    async def run_feed(self) -> None:
        cap = self._cfg.mirror_assets_per_connection
        conns: list[feed.FeedConnection] = []
        turn = 0
        async with asyncio.TaskGroup() as tg:
            while True:
                targets = list(self.state.by_asset)
                owned = set().union(*(c.assets for c in conns))
                gone = owned.difference(targets)
                new = [a for a in targets if a not in owned]
                if gone or new:
                    for conn in conns:
                        if ours := list(conn.assets & gone):
                            await conn.unsubscribe(ours)
                    while len(conns) * cap < len(targets):
                        conns.append(feed.FeedConnection(
                            self.state, self._cfg.mirror_watchdog_seconds))
                        tg.create_task(conns[-1].run())
                    plan: dict[feed.FeedConnection, list[str]] = {c: [] for c in conns}
                    for a in new:
                        conn = conns[turn % len(conns)]
                        while len(conn.assets) + len(plan[conn]) >= cap:
                            turn += 1
                            conn = conns[turn % len(conns)]
                        plan[conn].append(a)
                        turn += 1
                    for conn, assets in plan.items():
                        if assets:
                            await conn.subscribe(assets)
                    self._record_subscription(targets, connections=len(conns))
                for asset in self.state.emptied - self._checking:
                    if (ref := self.state.by_asset.get(asset)) is not None:
                        self._checking.add(asset)
                        tg.create_task(self._check_closure(ref))
                self.state.emptied.clear()
                await asyncio.sleep(FEED_SYNC_SECONDS)

    async def _check_closure(self, ref: MarketRef) -> None:
        try:
            for delay in (0.0, CLOSURE_RECHECK_SECONDS):
                await asyncio.sleep(delay)
                clob = await asyncio.to_thread(
                    clob_market, self._http, ref.pm_condition
                )
                if clob is None:
                    return
                if not clob.accepting:
                    await asyncio.to_thread(
                        transition,
                        self._db,
                        ref.market_id,
                        MarketState.ACTIVE,
                        MarketState.CLOSED,
                    )
                    return
                if (
                    clob.game_start is not None
                    and 0 <= time.time() - clob.game_start <= GAME_START_WINDOW_SECONDS
                ):
                    await asyncio.to_thread(
                        transition,
                        self._db,
                        ref.market_id,
                        MarketState.ACTIVE,
                        MarketState.ACTIVE,
                    )
                    return
        except Exception:
            log.exception("closure check for market %s failed", ref.market_id)
        finally:
            self._checking.discard(ref.pm_yes_token)

    def _record_subscription(self, assets: list[str], *, connections: int) -> None:
        self._subscribed = frozenset(assets)
        self._subscribed_at = self._clock()
        log.info("mirror feed subscribed %d assets over %d connections",
                 len(self._subscribed), connections)

    def _note_targets(self, now: float) -> None:
        """Start the clock for each target not yet subscribed, and forget
        assets that got subscribed or left the target set, so the map is
        bounded by the current unsubscribed targets."""
        missing = self.state.replicas.keys() - self._subscribed
        for asset in list(self._first_seen):
            if asset not in missing:
                del self._first_seen[asset]
        for asset in missing:
            self._first_seen.setdefault(asset, now)

    def feed_is_stale(self) -> bool:
        """True when the feed is dead or hung, not merely busy: a target has
        sat unsubscribed for longer than the threshold. The 2026-09-02 hang
        left every market created after 07:05 UTC (543 of them) on an empty
        book. Each asset is timed from when it first became a target, so a
        feed that picks up new markets within seconds is never stale, however
        many arrive.
        """
        now = self._clock()
        self._note_targets(now)
        threshold = max(3 * self._cfg.mirror_target_refresh_seconds, 60.0)
        overdue = [a for a, t in self._first_seen.items() if now - t > threshold]
        if not overdue:
            return False
        log.error(
            "mirror feed is idle with %d target assets unsubscribed for over "
            "%.0fs (last subscription %s)", len(overdue), threshold,
            "never" if self._subscribed_at is None
            else f"{now - self._subscribed_at:.0f}s ago")
        return True

    # ---- house side ------------------------------------------------------

    async def run_house(self) -> None:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(_every(
                "target refresh", self._cfg.mirror_target_refresh_seconds,
                self._refresh_targets))
            tg.create_task(_every(
                "sweep", SWEEP_SECONDS, lambda: asyncio.to_thread(self._orders.sweep)))
            tg.create_task(_every(
                "settlement check", SWEEP_SECONDS,
                lambda: asyncio.to_thread(self._orders.settle_pending, int(time.time()))))

    async def _refresh_targets(self) -> None:
        refs = await asyncio.to_thread(
            _load_refs, self._db, self._cfg.excluded_categories,
            self._cfg.excluded_tags,
        )
        self.state.set_targets(refs)
        # Start each new asset's clock the moment it becomes a target, not at
        # the next probe.
        self._note_targets(self._clock())

    async def run_tape(self) -> None:
        while True:
            prints: list[tape.MirroredPrint] = []
            while self.state.trades:
                ev = self.state.trades.popleft()
                ref = self.state.by_asset.get(ev["asset_id"])
                price = to_micro(ev.get("price"))
                size = to_micro(ev.get("size"))
                side = ev.get("side")
                try:
                    ts_s = int(ev.get("timestamp", "0")) // 1000
                except (TypeError, ValueError):
                    ts_s = 0
                if ref is None or price is None or size is None or size <= 0 \
                        or side not in ("BUY", "SELL") or ts_s <= 0 \
                        or not (0 < price < MICRO):
                    continue
                prints.append(
                    (
                        ref.condition_id,
                        ref.yes_token,
                        ref.no_token,
                        price,
                        size,
                        side,
                        ts_s,
                    )
                )
            if prints and self._cfg.mirror_tape_enabled:
                def _write():
                    with self._db.write() as conn:
                        tape.insert_mirrored_trades(conn, prints)
                try:
                    await asyncio.to_thread(_write)
                except Exception:
                    log.exception("mirror tape write failed, dropping %d prints",
                                  len(prints))
            await asyncio.sleep(1.0)
