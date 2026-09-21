# tests/liquidity/test_mirror_engine.py
import asyncio
import logging
from types import SimpleNamespace

import pytest

from agentpit.liquidity import mirror
from agentpit.liquidity.feed import MarketRef
from agentpit.liquidity.mirror import MirrorEngine


def _ref(i, pm):
    return MarketRef(market_id=i, condition_id=f"0xc{i}", yes_token=f"y{i}",
                     no_token=f"n{i}", pm_yes_token=pm)


class FlakyOrders:
    """cancel_market_orders raises N times, then succeeds."""
    def __init__(self, failures):
        self.failures = failures
        self.calls = []

    def cancel_market_orders(self, user, market, asset_id):
        self.calls.append(market)
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("transient cancel failure")


def _engine(monkeypatch, refs_box, orders):
    monkeypatch.setattr(
        mirror, "_load_refs", lambda db, cats=None, tags=None: refs_box["refs"]
    )
    eng = MirrorEngine.__new__(MirrorEngine)   # skip __init__ deps (db/onchain)
    eng._db = None
    # Only the fields `_refresh_targets` actually reads; the engine is built
    # by __new__ precisely to keep the real Settings/db/chain out of the test.
    eng._cfg = SimpleNamespace(excluded_categories=[], excluded_tags=[])
    eng._user = None
    eng._order = orders
    from agentpit.liquidity.feed import MirrorState
    eng.state = MirrorState([])
    eng._resubscribe = asyncio.Event()
    eng._pending_cancel = []
    eng._clock = lambda: 500.0
    eng._subscribed = frozenset()
    eng._first_seen = {}
    return eng


async def test_refresh_starts_each_new_targets_clock_when_it_arrives(monkeypatch):
    refs_box = {"refs": [_ref(1, "PM-A")]}
    eng = _engine(monkeypatch, refs_box, FlakyOrders(failures=0))
    await eng._refresh_targets()
    assert eng._first_seen == {"PM-A": 500.0}


async def test_refresh_signals_resubscribe_even_when_cancel_raises(monkeypatch):
    a, b = _ref(1, "PM-A"), _ref(2, "PM-B")
    refs_box = {"refs": [a, b]}
    orders = FlakyOrders(failures=1)
    eng = _engine(monkeypatch, refs_box, orders)

    await eng._refresh_targets()               # adds A+B
    assert eng._resubscribe.is_set()
    eng._resubscribe.clear()

    refs_box["refs"] = [b]                     # A removed; its cancel will RAISE
    await eng._refresh_targets()
    assert eng._resubscribe.is_set(), "signal must fire despite the failed cancel"
    assert orders.calls == ["0xc1"]            # attempted once

    refs_box["refs"] = [b]                     # no target change on next refresh
    eng._resubscribe.clear()
    await eng._refresh_targets()
    assert orders.calls == ["0xc1", "0xc1"], "failed cancel must be retried"
    assert not eng._pending_cancel, "retry succeeded — pending list drained"
    assert not eng._resubscribe.is_set()       # no change ⇒ no spurious rebuild


async def test_drain_tape_validates_price_range_and_caps(monkeypatch):
    a = _ref(1, "PM-A")
    refs_box = {"refs": [a]}
    eng = _engine(monkeypatch, refs_box, FlakyOrders(failures=0))
    await eng._refresh_targets()

    class Cfg:
        mirror_tape_enabled = True
    eng._cfg = Cfg()

    written = []
    monkeypatch.setattr(mirror.tape, "insert_mirrored_trade",
                        lambda conn, **kw: written.append(kw))

    class FakeConn:
        def __enter__(self): return self
        def __exit__(self, *args): return False

    class FakeDb:
        def write(self): return FakeConn()
    eng._db = FakeDb()

    def ev(price, size="10", side="BUY", ts="1700000000000"):
        return {"event_type": "last_trade_price", "asset_id": "PM-A",
                "price": price, "size": size, "side": side, "timestamp": ts}

    eng.state.trades.extend([ev("-0.5"), ev("1.5"), ev("0"), ev("0.48")])
    await eng._drain_tape()
    assert len(written) == 1 and written[0]["price_micro"] == 480_000

    eng.state.trades.extend(ev("0.5") for _ in range(250))
    await eng._drain_tape()
    assert len(written) == 1 + 200, "drain capped at 200/cycle"
    assert len(eng.state.trades) == 50, "remainder stays queued for next cycle"


# ---- _ref_of + fill_markets (sync->fill coupling) -------------------------


class _Cond:
    value = "0xcond7"


def test_ref_of_filters_and_builds():
    from agentpit.liquidity.mirror import _ref_of

    assert _ref_of(None) is None

    class _NoToken:
        polymarket_yes_token_id = None
        erc1155_tokens = [("y", "Up"), ("n", "Down")]

    assert _ref_of(_NoToken()) is None  # no upstream token -> not mirrorable

    class _NotBinary:
        polymarket_yes_token_id = "PM"
        erc1155_tokens = [("y", "Up")]

    assert _ref_of(_NotBinary()) is None

    class _Ok:
        market_id = 7
        polymarket_yes_token_id = "PM7"
        erc1155_tokens = [("y7", "Up"), ("n7", "Down")]
        condition_id = _Cond()

    r = _ref_of(_Ok())
    assert (r.market_id, r.pm_yes_token, r.yes_token, r.no_token, r.condition_id) == (
        7,
        "PM7",
        "y7",
        "n7",
        "0xcond7",
    )


class _ReadDb:
    def read(self):
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            yield "CONN"

        return _cm()


async def test_fill_markets_empty_is_noop():
    eng = MirrorEngine.__new__(MirrorEngine)
    assert await eng.fill_markets([]) == 0


async def test_fill_markets_seeds_then_reconciles(monkeypatch):
    eng = MirrorEngine.__new__(MirrorEngine)
    eng._db = _ReadDb()
    eng._order = object()
    eng._onchain = object()
    eng._user = object()
    eng._cfg = object()

    ref = _ref(7, "PM7")
    monkeypatch.setattr(mirror, "_ref_of", lambda m: ref)
    monkeypatch.setattr(mirror.TableRead, "read_market", lambda conn, mid: "MARKET")
    monkeypatch.setattr(
        mirror.feed,
        "fetch_books_rest",
        lambda assets: [{"asset_id": "PM7", "bids": [], "asks": []}],
    )

    class _Rep:
        def __init__(self, asset):
            pass

        def apply_book(self, ev):
            return True

        def snapshot(self):
            return "SNAP"

    monkeypatch.setattr(mirror, "BookReplica", _Rep)

    seen = {}

    def fake_reconcile(db, order, onchain, user, r, snap, cfg):
        seen["ref"] = r
        seen["snap"] = snap
        return {"placed": 5, "cancelled": 0}

    monkeypatch.setattr(mirror, "reconcile_market", fake_reconcile)

    placed = await eng.fill_markets([7])
    assert placed == 5
    assert seen["ref"] is ref and seen["snap"] == "SNAP"


# ---- feed staleness (the 2026-09-02 hang) ---------------------------------


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _stale_engine(refresh_seconds=10.0):
    eng = MirrorEngine.__new__(MirrorEngine)
    from agentpit.liquidity.feed import MirrorState
    eng.state = MirrorState([])
    eng._cfg = SimpleNamespace(mirror_target_refresh_seconds=refresh_seconds)
    eng._clock = _Clock()
    eng._subscribed = frozenset()
    eng._subscribed_at = None
    eng._first_seen = {}
    eng._rebuild_progress_at = None
    return eng


def test_feed_is_not_stale_right_after_a_rebuild():
    eng = _stale_engine()
    eng.state.set_targets([_ref(1, "PM-A")])
    eng._record_subscription(["PM-A"], connections=1)
    assert not eng.feed_is_stale()
    eng._clock.t += 3600
    assert not eng.feed_is_stale(), "steady state never turns stale"


def test_feed_turns_stale_only_after_a_new_target_stays_unsubscribed_past_threshold():
    eng = _stale_engine(refresh_seconds=10.0)      # threshold = max(30, 60) = 60s
    eng.state.set_targets([_ref(1, "PM-A")])
    eng._record_subscription(["PM-A"], connections=1)

    eng.state.set_targets([_ref(1, "PM-A"), _ref(2, "PM-B")])
    assert not eng.feed_is_stale()
    eng._clock.t += 59
    assert not eng.feed_is_stale(), "inside the threshold a rebuild is still due"
    eng._clock.t += 2
    assert eng.feed_is_stale()

    eng._record_subscription(["PM-A", "PM-B"], connections=1)
    assert not eng.feed_is_stale(), "a rebuild that subscribes it clears it"


def test_feed_stale_threshold_scales_with_the_target_refresh_interval():
    eng = _stale_engine(refresh_seconds=100.0)     # threshold = 300s
    eng.state.set_targets([_ref(1, "PM-A")])
    assert not eng.feed_is_stale()
    eng._clock.t += 200
    assert not eng.feed_is_stale()
    eng._clock.t += 101
    assert eng.feed_is_stale()


async def test_run_feed_records_and_logs_what_it_subscribed(monkeypatch, caplog):
    eng = _stale_engine()
    eng._cfg = SimpleNamespace(mirror_target_refresh_seconds=10.0,
                               mirror_watchdog_seconds=30.0,
                               mirror_assets_per_connection=2)
    eng._resubscribe = asyncio.Event()
    eng.state.set_targets([_ref(1, "PM-A"), _ref(2, "PM-B"), _ref(3, "PM-C")])

    async def no_seed(assets):
        return None

    async def hold(state, assets, watchdog_seconds):
        await asyncio.Event().wait()

    eng._seed_books = no_seed
    monkeypatch.setattr(mirror.feed, "run_connection", hold)
    caplog.set_level(logging.INFO, logger="agentpit.liquidity.mirror")

    task = asyncio.create_task(eng.run_feed())
    for _ in range(100):
        if eng._subscribed:
            break
        await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert eng._subscribed == {"PM-A", "PM-B", "PM-C"}
    assert eng._subscribed_at == eng._clock.t
    assert any("subscribed 3 assets over 2 connections" in r.getMessage()
               for r in caplog.records)


def _refs(names):
    return [_ref(i + 1, n) for i, n in enumerate(names)]


def test_steady_churn_of_new_markets_that_the_feed_picks_up_is_never_stale():
    # Prod: a new market every ~30s, subscribed ~10s after it appears. The old
    # probe timed ONE divergence window that only closed when a probe saw
    # nothing missing, so this healthy feed was judged stale after ~90s.
    eng = _stale_engine(refresh_seconds=15.0)      # threshold = 60s
    names = ["PM-0"]
    eng.state.set_targets(_refs(names))
    eng._record_subscription(names, connections=1)
    start = eng._clock.t
    arrivals = [start + 30.0 * i for i in range(1, 400)]   # ~3h20m of churn
    subscribed_at = {t: t + 10.0 for t in arrivals}
    # The supervisor's real cadence (30s), phase-locked inside each
    # subscription lag (the reviewer's trace probed at +5s): every probe
    # lands while SOME asset is missing, so no probe ever sees a clean set.
    probes = [t + off for t in arrivals for off in (5.0, 9.9)]
    events = sorted([(t, 0, "arrive") for t in arrivals]
                    + [(t, 1, "subscribe") for t in subscribed_at.values()]
                    + [(t, 2, "probe") for t in probes])
    verdicts = []
    for t, _, kind in events:
        eng._clock.t = t
        if kind == "arrive":
            names = names + [f"PM-{len(names)}"]
            eng.state.set_targets(_refs(names))
            eng._mark_progress()                   # the rebuild it triggers
        elif kind == "subscribe":
            eng._record_subscription(list(eng.state.replicas), connections=1)
            eng._rebuild_progress_at = None
        else:
            verdicts.append(eng.feed_is_stale())
    assert len(verdicts) > 700 and not any(verdicts)
    assert len(eng._first_seen) <= 1, "first-seen map holds only what is missing"


def test_steady_churn_is_not_stale_even_judged_as_idle():
    # Same churn with no rebuild marks at all: the per-asset clocks alone must
    # carry it, including probes that land inside every subscription lag.
    eng = _stale_engine(refresh_seconds=15.0)
    names = ["PM-0"]
    eng.state.set_targets(_refs(names))
    eng._record_subscription(names, connections=1)
    for i in range(1, 300):
        base = 1000.0 + 30.0 * i
        eng._clock.t = base
        names = names + [f"PM-{i}"]
        eng.state.set_targets(_refs(names))
        for off in (0.0, 5.0, 9.9):
            eng._clock.t = base + off
            assert not eng.feed_is_stale(), (i, off)
        eng._clock.t = base + 10.0
        eng._record_subscription(list(eng.state.replicas), connections=1)


def test_a_parked_feed_is_stale_once_the_oldest_new_target_passes_the_threshold():
    eng = _stale_engine(refresh_seconds=10.0)      # threshold = 60s
    names = ["PM-0"]
    eng.state.set_targets(_refs(names))
    eng._record_subscription(names, connections=1)
    first = eng._clock.t
    # Targets keep growing while the feed stays parked (the resubscribe signal
    # is ignored — the 2026-09-02 hang).
    for i in range(1, 20):
        eng._clock.t = first + 10.0 * (i - 1)
        names = names + [f"PM-{i}"]
        eng.state.set_targets(_refs(names))
        stale = eng.feed_is_stale()
        assert stale == (eng._clock.t - first > 60.0), eng._clock.t - first


def test_a_rebuild_that_keeps_making_progress_is_not_stale_however_long_it_takes():
    eng = _stale_engine(refresh_seconds=10.0)
    eng.state.set_targets(_refs([f"PM-{i}" for i in range(2000)]))
    eng._mark_progress()
    for _ in range(40):                            # 40 batches x 15s = 600s
        eng._clock.t += 15.0
        eng._mark_progress()
        assert not eng.feed_is_stale()
    assert all(eng._clock.t - t > 60 for t in eng._first_seen.values()), \
        "every asset is overdue by now — progress alone keeps it alive"


def test_a_rebuild_that_stops_making_progress_is_stale_after_the_timeout():
    eng = _stale_engine(refresh_seconds=10.0)
    eng.state.set_targets(_refs(["PM-A"]))
    eng._mark_progress()
    eng._clock.t += mirror.FEED_PROGRESS_TIMEOUT - 1
    assert not eng.feed_is_stale()
    eng._clock.t += 2
    assert eng.feed_is_stale()


def test_first_seen_forgets_subscribed_and_removed_assets():
    eng = _stale_engine()
    eng.state.set_targets(_refs(["PM-A", "PM-B", "PM-C"]))
    eng.feed_is_stale()
    assert set(eng._first_seen) == {"PM-A", "PM-B", "PM-C"}
    eng._record_subscription(["PM-A"], connections=1)
    eng.state.set_targets(_refs(["PM-A", "PM-B"]))
    eng.feed_is_stale()
    assert set(eng._first_seen) == {"PM-B"}


async def test_seed_runs_batch_by_batch_marking_progress_and_surviving_a_bad_batch(
        monkeypatch):
    eng = _stale_engine()
    assets = [f"PM-{i}" for i in range(250)]
    eng.state.set_targets(_refs(assets))
    calls, marks = [], []

    def fake_fetch(batch):
        calls.append(list(batch))
        marks.append(eng._rebuild_progress_at)
        eng._clock.t += 15.0
        if len(calls) == 2:
            raise RuntimeError("REST down")
        return [{"asset_id": a, "bids": [], "asks": []} for a in batch]

    monkeypatch.setattr(mirror.feed, "fetch_books_rest", fake_fetch)
    await eng._seed_books(assets)
    assert [len(c) for c in calls] == [100, 100, 50]
    assert marks[1:] == [1015.0, 1030.0], "progress is marked after each batch"
    assert eng._rebuild_progress_at == 1045.0


# -- the feed under supervise(), on scaled time: 1 ms real = 1 s simulated ---


def _scaled_feed_engine(monkeypatch, n_assets, batch_seconds, *, hang_after=None):
    """An engine whose clock runs 1000x and whose REST seed takes
    `batch_seconds` (simulated) per batch of 100, in a worker thread — as the
    real one does. `hang_after` makes every batch after that many block until
    the returned release event is set."""
    import threading
    import time as _time

    eng = _stale_engine(refresh_seconds=15.0)      # threshold = 60 "s"
    eng._cfg = SimpleNamespace(mirror_target_refresh_seconds=15.0,
                               mirror_watchdog_seconds=30.0,
                               mirror_assets_per_connection=500)
    eng._resubscribe = asyncio.Event()
    eng._clock = lambda: _time.monotonic() * 1000.0
    eng.state.set_targets(_refs([f"PM-{i}" for i in range(n_assets)]))
    release = threading.Event()
    fetched = []

    def fetch(batch):
        fetched.append(len(batch))
        if hang_after is not None and len(fetched) > hang_after:
            release.wait(2.0)
            return []
        # Simulated time scales with the assets asked for, so one big call
        # costs what the batches would have together.
        _time.sleep(batch_seconds / 1000.0 * -(-len(batch) // 100))
        return []

    async def hold(state, assets, watchdog_seconds):
        await asyncio.Event().wait()

    monkeypatch.setattr(mirror.feed, "fetch_books_rest", fetch)
    monkeypatch.setattr(mirror.feed, "run_connection", hold)
    return eng, fetched, release


async def _supervised(eng, starts):
    from agentpit.liquidity.supervise import supervise

    def factory():
        starts.append(1)
        return eng.run_feed()

    return asyncio.create_task(supervise(
        "mirror feed", factory, is_stale=eng.feed_is_stale,
        check_interval=0.005, restart_delay=0.001))


async def _stop_task(task):
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_slow_but_progressing_seed_subscribes_without_a_single_restart(
        monkeypatch):
    # 1500 assets = 15 batches x 15s = 225s of seed, far past the 60s
    # threshold. The old probe killed it mid-seed on every attempt and the
    # feed never subscribed again.
    eng, fetched, _ = _scaled_feed_engine(monkeypatch, 1500, batch_seconds=15.0)
    starts = []
    sup = await _supervised(eng, starts)
    try:
        await _until(lambda: eng._subscribed_at is not None, timeout=2.0)
    finally:
        await _stop_task(sup)
    assert starts == [1], "the progressing seed was never restarted"
    assert fetched == [100] * 15
    assert eng._subscribed == set(eng.state.replicas)


async def test_a_restarted_feed_is_not_killed_while_its_seed_progresses(monkeypatch):
    # After a supervisor restart the engine survives: every still-missing
    # asset's clock is long overdue and the killed instance left a stale
    # progress mark. The new instance must be left to finish its seed.
    eng, fetched, _ = _scaled_feed_engine(monkeypatch, 1000, batch_seconds=15.0)
    long_ago = eng._clock() - 10_000.0
    eng._first_seen = {a: long_ago for a in eng.state.replicas}
    eng._rebuild_progress_at = long_ago
    assert eng.feed_is_stale(), "as the supervisor found it before the restart"

    starts = []
    sup = await _supervised(eng, starts)
    try:
        await _until(lambda: eng._subscribed_at is not None, timeout=2.0)
    finally:
        await _stop_task(sup)
    assert starts == [1]
    assert fetched == [100] * 10
    assert not eng.feed_is_stale()
    assert eng._first_seen == {}, "subscribed assets are forgotten"


async def test_a_seed_that_stops_making_progress_is_restarted(monkeypatch):
    eng, fetched, release = _scaled_feed_engine(
        monkeypatch, 500, batch_seconds=5.0, hang_after=2)
    starts = []
    sup = await _supervised(eng, starts)
    try:
        await _until(lambda: len(starts) >= 2, timeout=2.0)
    finally:
        release.set()
        await _stop_task(sup)
    assert fetched[:3] == [100, 100, 100], "hung on the third batch"


async def test_a_feed_stuck_closing_its_connections_is_still_replaced(monkeypatch):
    # The supervisor restarts a stale feed by cancelling it. run_feed tears its
    # old connections down in a `finally` that swallowed CancelledError — the
    # connections' own, which is expected, but also the one aimed at run_feed.
    # A websocket close that hangs (a plausible form of the 2026-09-02 stall)
    # then ate the supervisor's cancel, and the supervisor waited on that feed
    # forever: supervision off, behind a task that looked alive.
    import time as _time

    eng, _, _ = _scaled_feed_engine(monkeypatch, 2, batch_seconds=1.0)

    async def slow_to_close(state, assets, watchdog_seconds):
        try:
            await asyncio.Event().wait()
        finally:
            # A close handshake that takes 200 "s" and will not be hurried.
            end = _time.monotonic() + 0.2
            while _time.monotonic() < end:
                try:
                    await asyncio.sleep(end - _time.monotonic())
                except asyncio.CancelledError:
                    pass

    monkeypatch.setattr(mirror.feed, "run_connection", slow_to_close)
    starts = []
    sup = await _supervised(eng, starts)
    try:
        await _until(lambda: eng._subscribed_at is not None, timeout=1.0)
        # A new market arrives; the rebuild begins by closing the old
        # connections, which now hangs for longer than the 60 "s" threshold.
        eng.state.set_targets(_refs(["PM-0", "PM-1", "PM-new"]))
        eng._note_targets(eng._clock())
        eng._resubscribe.set()
        await _until(lambda: len(starts) >= 2, timeout=2.0)
        await _until(lambda: "PM-new" in eng._subscribed, timeout=2.0)
    finally:
        await _stop_task(sup)


async def test_run_feed_honours_a_cancel_even_when_a_connection_fails_to_close(
        monkeypatch):
    # The same lost cancel by another road: a connection whose close raises an
    # ordinary error rather than CancelledError. That error left run_feed's
    # teardown, landed in "mirror feed cycle failed — retrying", and the feed
    # went on rebuilding itself with the supervisor's cancel gone.
    import time as _time

    eng, _, _ = _scaled_feed_engine(monkeypatch, 2, batch_seconds=1.0)
    closes = []

    async def first_close_fails(state, assets, watchdog_seconds):
        try:
            await asyncio.Event().wait()
        finally:
            closes.append(1)
            if len(closes) == 1:
                end = _time.monotonic() + 0.05
                while _time.monotonic() < end:
                    try:
                        await asyncio.sleep(end - _time.monotonic())
                    except asyncio.CancelledError:
                        pass
                raise ConnectionError("close handshake failed")
            # Later closes are clean, so a regressed build that loops past the
            # cancel can still be stopped once the assertion has failed.

    monkeypatch.setattr(mirror.feed, "run_connection", first_close_fails)
    feed = asyncio.create_task(eng.run_feed())
    try:
        await _until(lambda: eng._subscribed_at is not None, timeout=1.0)
        eng._resubscribe.set()               # rebuild: close the connections
        await _until(lambda: closes, timeout=1.0)
        feed.cancel()                        # ...and cancel mid-close
        await asyncio.wait({feed}, timeout=1.0)
        assert feed.done() and feed.cancelled(), "the cancel was swallowed"
    finally:
        if not feed.done():
            feed.cancel()
            await asyncio.wait({feed}, timeout=1.0)


async def _until(pred, timeout=1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.002)


# ---- reconciler: one failing market must not sink the pass ----------------


class _Snap:
    def __init__(self, asset):
        self.asset = asset

    def snapshot(self):
        return f"SNAP-{self.asset}"


async def test_a_market_whose_reconcile_raises_is_retried_and_does_not_skip_the_rest(
        monkeypatch, caplog):
    from agentpit.liquidity.feed import MirrorState
    eng = MirrorEngine.__new__(MirrorEngine)
    eng._db = eng._order = eng._onchain = eng._user = None
    # Long sleeps between passes, so everything asserted below happened in ONE
    # pass: under the old code the second market would only be reached on a
    # later pass, 10s away.
    eng._cfg = SimpleNamespace(
        mirror_target_refresh_seconds=3600.0,
        mirror_reconcile_min_interval_seconds=10.0,
        liquidity_interval_seconds=10.0,
        mirror_cold_interval_seconds=0.0,
        mirror_tape_enabled=False,
    )
    eng._cold_priority = {}
    eng._pending_cancel = []
    eng.state = MirrorState([_ref(1, "PM-A"), _ref(2, "PM-B")])
    eng.state.replicas = {"PM-A": _Snap("PM-A"), "PM-B": _Snap("PM-B")}
    eng.state.dirty = {"PM-A", "PM-B"}

    async def no_refresh():
        return None

    eng._refresh_targets = no_refresh

    calls = []

    def fake_reconcile(db, order, onchain, user, ref, snap, cfg, cold=False):
        calls.append(ref.pm_yes_token)
        if len(calls) == 1:
            raise ConnectionError("RemoteDisconnected")
        return {"placed": 0, "cancelled": 0, "deferred": 0, "failed": 0}

    monkeypatch.setattr(mirror, "reconcile_market", fake_reconcile)
    caplog.set_level(logging.ERROR, logger="agentpit.liquidity.mirror")

    task = asyncio.create_task(eng.run_reconciler())
    for _ in range(200):
        if len(calls) == 2:
            break
        await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    failed, other = calls[0], calls[1:]
    assert other and other[0] != failed, "the next market is reconciled in the same pass"
    assert eng.state.dirty == {failed}, "the failing market goes back to dirty"
    failed_id = eng.state.by_asset[failed].market_id
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any(f"market {failed_id}" in r.getMessage() and r.exc_info
               for r in errors)
