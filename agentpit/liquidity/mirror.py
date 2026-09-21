# agentpit/liquidity/mirror.py
"""MirrorEngine — glue between the WSS feed, the reconciler, and the tape.

Two lifespan tasks (siblings of polymarket_sync / snapshot):
  run_feed       — REST-seeds replicas, then holds sharded WSS connections.
  run_reconciler — drains dirty markets (coalesced per-market) and the trade
                   queue; refreshes the target market set; cancels orders on
                   the ACTIVE→gone edge (resolution/cancellation).
Blocking work (DB/chain/REST) runs via asyncio.to_thread.
"""
import asyncio
import logging
import time

from agentpit.config import Settings
from agentpit.datastructures.user import User
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.liquidity import feed, tape
from agentpit.liquidity.feed import MarketRef, MirrorState
from agentpit.liquidity.reconciler import reconcile_market
from agentpit.liquidity.replica import MICRO, BookReplica, to_micro
from agentpit.onchain.admin import OnchainAdmin
from agentpit.services.order_service import OrderService

log = logging.getLogger(__name__)


def _ref_of(m) -> "MarketRef | None":
    """Build a MarketRef from a Market, or None if it can't be mirrored (no
    upstream token, or not binary)."""
    if m is None or not m.polymarket_yes_token_id or len(m.erc1155_tokens) < 2:
        return None
    return MarketRef(
        market_id=m.market_id,
        condition_id=m.condition_id.value,
        yes_token=m.erc1155_tokens[0][0],
        no_token=m.erc1155_tokens[1][0],
        pm_yes_token=m.polymarket_yes_token_id,
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


def cold_seed(priority: float, interval: float, now: float) -> float:
    """Initial `last_cold` for an asset, placed in the queue by priority.

    Without an offset every market is due for its first cold sweep the moment
    the process starts, and a boot would place the whole deep book for every
    market at once. So the first sweeps are spread across one interval — but
    WHERE in that interval a market lands is a decision, and it used to be a
    hash of the asset id, which is to say a coin toss.

    `priority` is 1.0 for the market that should deepen first and 0.0 for the
    one that can wait a full interval; the mirror derives it from 24h volume,
    so the books a person is most likely to open fill in first. The offset is
    a fraction of the interval rather than `% int(interval)`, so this is
    well-defined for any positive interval — including sub-second ones, where
    the integer modulo raised `ZeroDivisionError`, and ones in `[1, 2)`, where
    it collapsed to a constant 0 and defeated the stagger.
    """
    if interval <= 0:
        return now
    return now - max(0.0, min(1.0, priority)) * interval


def cold_due(last_cold: float, interval: float, now: float) -> bool:
    """Are this market's deep levels due for a sweep? `interval <= 0` disables
    the cold tier entirely, so every pass stays hot."""
    return interval > 0 and now - last_cold >= interval


# REST /books batch size for the feed's seed (feed.fetch_books_rest's default).
SEED_BATCH_SIZE = 100
# A rebuild with no sign of progress for this long is hung. Comfortably above
# one seed batch's 15s httpx timeout, so a batch that times out is not a hang.
FEED_PROGRESS_TIMEOUT = 60.0


class MirrorEngine:
    def __init__(self, db: DbSession, onchain: OnchainAdmin,
                 settings: Settings, user: User):
        self._db = db
        self._onchain = onchain
        self._cfg = settings
        self._user = user
        self._order = OrderService(db, onchain)
        self.state = MirrorState([])
        # asset -> [0,1] priority for its FIRST cold sweep; rebuilt whenever
        # the target set is reloaded, so a market that climbs the volume
        # ranking moves up the queue the next time it is seeded.
        self._cold_priority: "dict[str, float]" = {}
        self._resubscribe = asyncio.Event()
        self._pending_cancel: list[MarketRef] = []
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
        # Last sign of life from a rebuild in progress (seed batch done,
        # connections started); None while the feed is parked on a
        # subscription, waiting for the next target change.
        self._rebuild_progress_at: "float | None" = None

    # ---- feed side -------------------------------------------------------

    async def run_feed(self) -> None:
        # A fresh feed (first start, or a supervisor restart after a hang)
        # starts its rebuild with a fresh progress clock. The first-seen clocks
        # survive a restart, so every still-missing asset may already be
        # overdue — what keeps the new instance alive is its seed progressing.
        self._rebuild_progress_at = None
        while True:
            try:
                assets = list(self.state.replicas)
                self._resubscribe.clear()
                if not assets:
                    self._rebuild_progress_at = None     # parked, nothing to do
                    await self._wait_resubscribe(self._cfg.mirror_target_refresh_seconds)
                    continue
                if self._rebuild_progress_at is None:
                    # A retry after a failed cycle keeps the old mark: a
                    # rebuild that fails over and over is not progressing.
                    self._mark_progress()
                await self._seed_books(assets)
                conns = [
                    asyncio.create_task(feed.run_connection(
                        self.state, shard_assets,
                        watchdog_seconds=self._cfg.mirror_watchdog_seconds))
                    for shard_assets in feed.shard(
                        assets, self._cfg.mirror_assets_per_connection)
                ]
                self._record_subscription(assets, connections=len(conns))
                self._rebuild_progress_at = None         # parked on a live feed
                try:
                    await self._resubscribe.wait()   # target set changed — rebuild
                finally:
                    for t in conns:
                        t.cancel()
                    # Every connection's outcome is collected — cancelled or
                    # a failed close alike — so none escapes as an error, and
                    # none is left unretrieved or still tearing down beside
                    # the next rebuild. A cancel aimed at run_feed itself (the
                    # supervisor replacing a stale feed) is not swallowed:
                    # gather raises it here once the connections have ended.
                    await asyncio.gather(*conns, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                me = asyncio.current_task()
                if me is not None and me.cancelling():
                    # Whatever failed, a cancel is pending — honour it rather
                    # than retry, or the supervisor waits on this feed forever.
                    raise asyncio.CancelledError from exc
                log.exception("mirror feed cycle failed — retrying")
                await asyncio.sleep(2.0)

    def _record_subscription(self, assets: list[str], *, connections: int) -> None:
        self._subscribed = frozenset(assets)
        self._subscribed_at = self._clock()
        log.info("mirror feed subscribed %d assets over %d connections",
                 len(self._subscribed), connections)

    def _mark_progress(self) -> None:
        self._rebuild_progress_at = self._clock()

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
        """True when the feed is dead or hung, not merely busy.

        Two ways to be stale — the 2026-09-02 hang left every market created
        after 07:05 UTC (543 of them) on an empty book:

        * Parked while a target has sat unsubscribed for longer than the
          threshold: the resubscribe signal was not honoured. Each asset is
          timed from when it first became a target, so a feed that picks up
          new markets within seconds is never stale, however many arrive.
        * Rebuilding (REST seed, then connections) with no progress for
          FEED_PROGRESS_TIMEOUT. A slow seed that keeps finishing batches is
          left alone, however long it takes — killing it would only restart
          the same seed and the feed would never subscribe again.
        """
        now = self._clock()
        self._note_targets(now)
        progress_at = self._rebuild_progress_at
        if progress_at is not None:
            idle = now - progress_at
            if idle <= FEED_PROGRESS_TIMEOUT:
                return False
            log.error("mirror feed rebuild has made no progress for %.0fs "
                      "(%d target assets unsubscribed)", idle,
                      len(self._first_seen))
            return True
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

    async def _seed_books(self, assets: list[str]) -> None:
        """REST-seed book snapshots for `assets` into the shared feed state,
        marking each fresh book dirty for the reconciler. Used for the initial
        seed on every (re)subscribe.

        One worker thread per batch, awaited from here, so the rebuild reports
        progress after every batch and a cancelled seed leaves at most the one
        in-flight batch running (bounded by its 15s HTTP timeout) rather than
        a whole thousand-asset seed."""
        for batch in feed.shard(assets, SEED_BATCH_SIZE):
            try:
                books = await asyncio.to_thread(feed.fetch_books_rest, batch)
            except Exception:
                log.exception("mirror seed failed for a batch of %d", len(batch))
                books = []
            for b in books:
                self.state.handle_event({**b, "event_type": "book"})
            self._mark_progress()

    async def fill_markets(self, market_ids: list[int]) -> int:
        """Immediately seed + reconcile the given markets (off-thread), so a
        freshly-synced market is liquid at once instead of waiting for the
        discovery loop to reach it.

        Couples market sync to the liquidity fill — what makes fast-rotating
        windows (live for only ~5 min) tradeable the moment they sync. Runs
        entirely in one worker thread (DB read, REST book fetch, order
        placement), so the event loop / API stay responsive. Self-contained:
        builds its own replicas/snapshots and does not touch the live feed
        state, which independently maintains the markets thereafter. Returns the
        number of orders placed.
        """
        if not market_ids:
            return 0

        def _work() -> int:
            with self._db.read() as conn:
                refs = [
                    ref
                    for ref in (
                        _ref_of(TableRead.read_market(conn, mid))
                        for mid in market_ids
                    )
                    if ref is not None
                ]
            if not refs:
                return 0
            by_asset = {
                b.get("asset_id"): b
                for b in feed.fetch_books_rest([r.pm_yes_token for r in refs])
            }
            placed = 0
            for ref in refs:
                book = by_asset.get(ref.pm_yes_token)
                if book is None:
                    continue  # upstream window not (yet) tradeable — nothing to mirror
                rep = BookReplica(ref.pm_yes_token)
                rep.apply_book({**book, "event_type": "book"})
                snap = rep.snapshot()
                if snap is None:
                    continue
                stats = reconcile_market(
                    self._db, self._order, self._onchain, self._user, ref, snap,
                    self._cfg)
                placed += stats["placed"]
                if stats["placed"] or stats["cancelled"]:
                    log.info("fill market %s: %s", ref.market_id, stats)
            return placed

        return await asyncio.to_thread(_work)

    async def _wait_resubscribe(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._resubscribe.wait(), timeout)
        except TimeoutError:
            pass

    # ---- reconcile side --------------------------------------------------

    async def run_reconciler(self) -> None:
        last_run: dict[str, float] = {}
        last_cold: dict[str, float] = {}
        last_refresh = 0.0
        while True:
            try:
                now = asyncio.get_running_loop().time()
                if now - last_refresh >= self._cfg.mirror_target_refresh_seconds:
                    last_refresh = now
                    await self._refresh_targets()
                await self._drain_tape()
                ready = [
                    a for a in list(self.state.dirty)
                    if now - last_run.get(a, 0.0)
                    >= self._cfg.mirror_reconcile_min_interval_seconds
                ]
                for asset in ready:
                    self.state.dirty.discard(asset)
                    ref = self.state.by_asset.get(asset)
                    rep = self.state.replicas.get(asset)
                    snap = rep.snapshot() if rep is not None else None
                    if ref is None or snap is None:
                        continue
                    interval = self._cfg.mirror_cold_interval_seconds
                    if asset not in last_cold:
                        last_cold[asset] = cold_seed(
                            # Unknown asset (seen by the feed before the next
                            # target reload) goes to the back rather than the
                            # front: a market nobody ranked is not urgent.
                            self._cold_priority.get(asset, 0.0), interval, now
                        )
                    cold = cold_due(last_cold[asset], interval, now)
                    if cold:
                        last_cold[asset] = now
                    last_run[asset] = now
                    try:
                        stats = await asyncio.to_thread(
                            reconcile_market, self._db, self._order,
                            self._onchain, self._user, ref, snap, self._cfg,
                            cold=cold)
                    except Exception:
                        # One market's chain call failing (the SKALE RPC threw
                        # RemoteDisconnected 1,347 times on 2026-09-17/18)
                        # used to abort the whole pass: this market left the
                        # dirty set for good — a quiet one may never change
                        # upstream again — and every market after it waited
                        # for the next pass. Retry it, and keep going.
                        log.exception("mirror reconcile for market %s failed "
                                      "— will retry", ref.market_id)
                        self.state.dirty.add(asset)
                        continue
                    if stats["deferred"] or stats["failed"]:
                        # Incomplete cycle — converge on a later pass even if
                        # no new upstream event arrives.
                        self.state.dirty.add(asset)
                    if stats["placed"] or stats["cancelled"]:
                        log.info("mirror market %s: %s", ref.market_id, stats)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("mirror reconcile cycle failed")
            await asyncio.sleep(
                self._cfg.mirror_reconcile_min_interval_seconds
                if self.state.dirty or self.state.trades
                else self._cfg.liquidity_interval_seconds)

    async def _refresh_targets(self) -> None:
        refs = await asyncio.to_thread(
            _load_refs, self._db, self._cfg.excluded_categories,
            self._cfg.excluded_tags,
        )
        # `_load_refs` returns them busiest first, and that order is the whole
        # point: it becomes each market's place in the cold-sweep queue.
        n = max(1, len(refs))
        self._cold_priority = {
            r.pm_yes_token: 1.0 - i / n for i, r in enumerate(refs)
        }
        added, removed = self.state.set_targets(refs)
        # Start each new asset's clock the moment it becomes a target, not at
        # the next probe.
        self._note_targets(self._clock())
        # An excluded market leaves the target set exactly as a resolved one
        # does, so `removed` carries it into the cancel pass below and the
        # orders it already has are withdrawn — the catalogue and the book stop
        # showing it in the same pass.
        if added or removed:
            # Signal BEFORE any fallible work — a lost signal would leave new
            # markets unsubscribed until the next unrelated target change.
            self._resubscribe.set()
        self._pending_cancel.extend(removed)
        if not self._pending_cancel:
            return
        still_pending: list[MarketRef] = []
        for ref in self._pending_cancel:
            try:
                await asyncio.to_thread(
                    self._order.cancel_market_orders, self._user,
                    ref.condition_id, None)
                log.info("market %s left the active set — mirror orders cancelled",
                         ref.market_id)
            except Exception:
                log.exception("cancel for removed market %s failed — will retry",
                              ref.market_id)
                still_pending.append(ref)
        self._pending_cancel = still_pending

    async def _drain_tape(self) -> None:
        if not self._cfg.mirror_tape_enabled:
            self.state.trades.clear()
            return
        for _ in range(200):
            if not self.state.trades:
                break
            ev = self.state.trades.popleft()
            ref = self.state.by_asset.get(ev.get("asset_id"))
            price = to_micro(ev.get("price"))
            size = to_micro(ev.get("size"))
            side = ev.get("side")
            try:
                ts_s = int(ev.get("timestamp", "0")) // 1000   # WSS gives ms
            except (TypeError, ValueError):
                ts_s = 0
            if ref is None or price is None or size is None or size <= 0 \
                    or side not in ("BUY", "SELL") or ts_s <= 0 \
                    or not (0 < price < MICRO):
                continue
            def _write(ref=ref, price=price, size=size, side=side, ts_s=ts_s):
                with self._db.write() as conn:
                    tape.insert_mirrored_trade(
                        conn, condition_id=ref.condition_id,
                        local_token_id=ref.yes_token, price_micro=price,
                        size_micro=size, side=side, match_time_s=ts_s)
            try:
                await asyncio.to_thread(_write)
            except Exception:
                log.exception("mirror tape write failed (market=%s) — dropping event",
                              ref.market_id)
