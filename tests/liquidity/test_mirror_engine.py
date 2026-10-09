# tests/liquidity/test_mirror_engine.py
import asyncio
import logging
import threading
import time
from types import SimpleNamespace

import pytest

from agentpit.datastructures.market_state import MarketState
from agentpit.liquidity import mirror
from agentpit.liquidity.feed import MarketRef, MirrorState
from agentpit.liquidity.mirror import MirrorEngine
from agentpit.polymarket.polymarket_sync import Clob


def _ref(i, pm):
    return MarketRef(market_id=i, condition_id=f"0xc{i}", yes_token=f"y{i}",
                     no_token=f"n{i}", pm_yes_token=pm, pm_condition=f"0xpm{i}")


def _engine(monkeypatch, refs_box):
    monkeypatch.setattr(
        mirror, "_load_refs", lambda db, cats=None, tags=None: refs_box["refs"]
    )
    eng = MirrorEngine.__new__(MirrorEngine)   # skip __init__ deps (db/onchain)
    eng._db = None
    # Only the fields `_refresh_targets` actually reads; the engine is built
    # by __new__ precisely to keep the real Settings/db/chain out of the test.
    eng._cfg = SimpleNamespace(excluded_categories=[], excluded_tags=[])
    eng.state = MirrorState([])
    eng._clock = lambda: 500.0
    eng._subscribed = frozenset()
    eng._first_seen = {}
    return eng


async def test_refresh_starts_each_new_targets_clock_when_it_arrives(monkeypatch):
    refs_box = {"refs": [_ref(1, "PM-A")]}
    eng = _engine(monkeypatch, refs_box)
    await eng._refresh_targets()
    assert eng._first_seen == {"PM-A": 500.0}


async def test_a_stuck_sweep_holds_up_neither_refresh_nor_settlement(monkeypatch):
    refs_box = {"refs": [_ref(1, "PM-A")]}
    eng = _engine(monkeypatch, refs_box)
    eng._cfg.mirror_target_refresh_seconds = 0.01
    monkeypatch.setattr(mirror, "SWEEP_SECONDS", 0.01)
    stuck = threading.Event()
    settled: list[int] = []
    eng._orders = SimpleNamespace(sweep=stuck.wait, settle_pending=settled.append)
    task = asyncio.create_task(eng.run_house())
    await asyncio.sleep(0.05)
    refs_box["refs"] = [_ref(2, "PM-B")]
    await asyncio.sleep(0.05)
    stuck.set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(eng.state.by_asset) == ["PM-B"]
    assert len(settled) > 1


async def test_tape_writer_writes_every_valid_queued_print_in_one_batch(monkeypatch):
    eng = MirrorEngine.__new__(MirrorEngine)
    eng._cfg = SimpleNamespace(mirror_tape_enabled=True)
    eng.state = MirrorState([_ref(1, "PM-A")])
    batches = []
    monkeypatch.setattr(mirror.tape, "insert_mirrored_trades",
                        lambda conn, prints: batches.append(prints))

    class FakeConn:
        def __enter__(self): return self
        def __exit__(self, *args): return False

    class FakeDb:
        def write(self): return FakeConn()
    eng._db = FakeDb()

    def ev(price, asset="PM-A", side="BUY", ts="1700000000000"):
        return {"event_type": "last_trade_price", "asset_id": asset,
                "price": price, "size": "10", "side": side, "timestamp": ts}

    eng.state.trades.extend([ev("-0.5"), ev("1.5"), ev("0"), ev("0.5", asset="PM-X"),
                             ev("0.5", side="HOLD"), ev("0.5", ts="soon")])
    eng.state.trades.extend(ev("0.48") for _ in range(250))
    task = asyncio.create_task(eng.run_tape())
    await _until(lambda: batches)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert batches == [[("0xc1", "y1", "n1", 480_000, 10_000_000, "BUY", 1_700_000_000)] * 250]
    assert not eng.state.trades


# ---- _ref_of -------------------------------------------------------------


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
        polymarket_condition_id = "0xpm"
        erc1155_tokens = [("y", "Up")]

    assert _ref_of(_NotBinary()) is None

    class _NoCondition:
        polymarket_yes_token_id = "PM"
        polymarket_condition_id = None
        erc1155_tokens = [("y", "Up"), ("n", "Down")]

    assert _ref_of(_NoCondition()) is None

    class _Ok:
        market_id = 7
        polymarket_yes_token_id = "PM7"
        polymarket_condition_id = "0xpm7"
        erc1155_tokens = [("y7", "Up"), ("n7", "Down")]
        condition_id = _Cond()

    r = _ref_of(_Ok())
    assert (r.market_id, r.pm_yes_token, r.yes_token, r.no_token, r.condition_id, r.pm_condition) == (
        7,
        "PM7",
        "y7",
        "n7",
        "0xcond7",
        "0xpm7",
    )


# ---- feed staleness (the 2026-09-02 hang) ---------------------------------


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _stale_engine(refresh_seconds=10.0):
    eng = MirrorEngine.__new__(MirrorEngine)
    eng.state = MirrorState([])
    eng._cfg = SimpleNamespace(mirror_target_refresh_seconds=refresh_seconds)
    eng._clock = _Clock()
    eng._subscribed = frozenset()
    eng._subscribed_at = None
    eng._first_seen = {}
    eng._checking = set()
    return eng


def test_feed_is_not_stale_right_after_a_subscription():
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
    assert not eng.feed_is_stale(), "inside the threshold placement is still due"
    eng._clock.t += 2
    assert eng.feed_is_stale()

    eng._record_subscription(["PM-A", "PM-B"], connections=1)
    assert not eng.feed_is_stale(), "placing it clears it"


def test_feed_stale_threshold_scales_with_the_target_refresh_interval():
    eng = _stale_engine(refresh_seconds=100.0)     # threshold = 300s
    eng.state.set_targets([_ref(1, "PM-A")])
    assert not eng.feed_is_stale()
    eng._clock.t += 200
    assert not eng.feed_is_stale()
    eng._clock.t += 101
    assert eng.feed_is_stale()


async def test_run_feed_stripes_by_volume_then_places_changes_in_place(
        monkeypatch, caplog):
    eng = _stale_engine()
    eng._cfg = SimpleNamespace(mirror_target_refresh_seconds=10.0,
                               mirror_watchdog_seconds=30.0,
                               mirror_assets_per_connection=2)
    eng.state.set_targets(_refs(["A", "B", "C", "D", "E"]))
    conns = []

    async def hold(conn):
        conns.append(conn)
        await asyncio.Event().wait()

    monkeypatch.setattr(mirror.feed.FeedConnection, "run", hold)
    monkeypatch.setattr(mirror, "FEED_SYNC_SECONDS", 0.005)
    caplog.set_level(logging.INFO, logger="agentpit.liquidity.mirror")

    task = asyncio.create_task(eng.run_feed())
    await _until(lambda: len(conns) == 3)
    assert [c.assets for c in conns] == [{"A", "D"}, {"B", "E"}, {"C"}]
    assert eng._subscribed == {"A", "B", "C", "D", "E"}
    assert eng._subscribed_at == eng._clock.t
    assert any("subscribed 5 assets over 3 connections" in r.getMessage()
               for r in caplog.records)

    eng.state.set_targets(_refs(["A", "C", "D", "E", "F", "G"]))
    await _until(lambda: "G" in eng._subscribed)
    assert [c.assets for c in conns] == [{"A", "D"}, {"E", "G"}, {"C", "F"}]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


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
        elif kind == "subscribe":
            eng._record_subscription(list(eng.state.replicas), connections=1)
        else:
            verdicts.append(eng.feed_is_stale())
    assert len(verdicts) > 700 and not any(verdicts)
    assert len(eng._first_seen) <= 1, "first-seen map holds only what is missing"


def test_a_parked_feed_is_stale_once_the_oldest_new_target_passes_the_threshold():
    eng = _stale_engine(refresh_seconds=10.0)      # threshold = 60s
    names = ["PM-0"]
    eng.state.set_targets(_refs(names))
    eng._record_subscription(names, connections=1)
    first = eng._clock.t
    # Targets keep growing while the feed places none of them (the 2026-09-02 hang).
    for i in range(1, 20):
        eng._clock.t = first + 10.0 * (i - 1)
        names = names + [f"PM-{i}"]
        eng.state.set_targets(_refs(names))
        stale = eng.feed_is_stale()
        assert stale == (eng._clock.t - first > 60.0), eng._clock.t - first


def test_first_seen_forgets_subscribed_and_removed_assets():
    eng = _stale_engine()
    eng.state.set_targets(_refs(["PM-A", "PM-B", "PM-C"]))
    eng.feed_is_stale()
    assert set(eng._first_seen) == {"PM-A", "PM-B", "PM-C"}
    eng._record_subscription(["PM-A"], connections=1)
    eng.state.set_targets(_refs(["PM-A", "PM-B"]))
    eng.feed_is_stale()
    assert set(eng._first_seen) == {"PM-B"}


async def test_run_feed_honours_a_cancel_even_when_a_connection_fails_to_close(
        monkeypatch):
    connected = []

    class CloseFails:
        async def __aenter__(self):
            connected.append(1)
            return self

        async def __aexit__(self, *exc):
            raise ConnectionError("close handshake failed")

        async def send(self, _msg):
            pass

        async def recv(self):
            await asyncio.Event().wait()

    eng = _stale_engine()
    eng._cfg = SimpleNamespace(mirror_target_refresh_seconds=10.0,
                               mirror_watchdog_seconds=30.0,
                               mirror_assets_per_connection=2)
    eng.state.set_targets(_refs(["PM-0", "PM-1"]))
    monkeypatch.setattr(mirror.feed, "connect", lambda url, **kw: CloseFails())
    feed_task = asyncio.create_task(eng.run_feed())
    await _until(lambda: connected)
    feed_task.cancel()
    await asyncio.wait({feed_task}, timeout=1.0)
    assert feed_task.done() and feed_task.cancelled(), "the cancel was swallowed"
    assert connected == [1]


async def _until(pred, timeout=1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.002)


def _closure_engine(monkeypatch, answers):
    eng = _stale_engine()
    eng._db = "db"
    eng._http = "http"
    eng.state.set_targets([_ref(7, "PM-A")])
    asked, moved = [], []

    def clob_market(http, condition_id):
        asked.append(condition_id)
        return answers.pop(0)

    monkeypatch.setattr(mirror, "clob_market", clob_market)
    monkeypatch.setattr(mirror, "transition", lambda db, *args: moved.append(args))
    monkeypatch.setattr(mirror, "CLOSURE_RECHECK_SECONDS", 0.0)
    return eng, asked, moved


@pytest.mark.parametrize(
    ("answers", "asks", "moves"),
    [
        pytest.param([Clob(False, None)], 1, [(7, MarketState.ACTIVE, MarketState.CLOSED)], id="ao absent closes"),
        pytest.param([None], 1, [], id="an error leaves it to the sweep"),
        pytest.param([Clob(True, None), Clob(False, None)], 2, [(7, MarketState.ACTIVE, MarketState.CLOSED)], id="the re-check closes"),
        pytest.param([Clob(True, None), Clob(True, None)], 2, [], id="still accepting after the re-check"),
        pytest.param([Clob(True, -30)], 1, [(7, MarketState.ACTIVE, MarketState.ACTIVE)], id="game start clears the book"),
        pytest.param([Clob(True, -86_400), Clob(True, -86_400)], 2, [], id="a game long under way is not a kickoff"),
    ],
)
async def test_closure_check(monkeypatch, answers, asks, moves):
    now = int(time.time())
    answers = [
        a._replace(game_start=now + a.game_start) if a is not None and a.game_start is not None else a
        for a in answers
    ]
    eng, asked, moved = _closure_engine(monkeypatch, answers)
    eng._checking.add("PM-A")

    await eng._check_closure(eng.state.by_asset["PM-A"])

    assert asked == ["0xpm7"] * asks
    assert moved == moves
    assert eng._checking == set()


async def test_run_feed_checks_each_emptied_book_once(monkeypatch):
    eng, asked, moved = _closure_engine(monkeypatch, [Clob(False, None)])
    eng._cfg = SimpleNamespace(mirror_target_refresh_seconds=10.0,
                               mirror_watchdog_seconds=30.0,
                               mirror_assets_per_connection=2)

    async def hold(conn):
        await asyncio.Event().wait()

    monkeypatch.setattr(mirror.feed.FeedConnection, "run", hold)
    monkeypatch.setattr(mirror, "FEED_SYNC_SECONDS", 0.005)
    eng.state.emptied.add("PM-A")
    task = asyncio.create_task(eng.run_feed())
    await _until(lambda: moved)
    await _until(lambda: not eng._checking)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (asked, moved, eng.state.emptied) == (["0xpm7"], [(7, MarketState.ACTIVE, MarketState.CLOSED)], set())
