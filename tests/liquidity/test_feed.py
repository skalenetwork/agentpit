import asyncio
import json
import logging

import pytest
from websockets.exceptions import ConnectionClosedOK
from websockets.frames import Close

from agentpit.liquidity import feed
from agentpit.liquidity.feed import (
    FeedConnection, MarketRef, MirrorState, parse_events, shard,
)


def _ref(pm="PM-YES", market_id=1):
    return MarketRef(market_id=market_id, condition_id=f"0xc{market_id}",
                     yes_token=f"y{market_id}", no_token=f"n{market_id}",
                     pm_yes_token=pm, pm_condition=f"0xpm{market_id}")


def test_parse_events_handles_both_framings_and_garbage():
    assert parse_events('{"event_type":"book"}') == [{"event_type": "book"}]
    assert parse_events('[{"a":1},{"b":2}]') == [{"a": 1}, {"b": 2}]
    assert parse_events("PONG") == []
    assert parse_events("[1,2]") == []


def test_shard():
    assert shard(list(range(5)), 2) == [[0, 1], [2, 3], [4]]
    assert shard([], 2) == []


def test_state_routes_book_and_price_change():
    st = MirrorState([_ref()])
    st.handle_event({"event_type": "book", "asset_id": "PM-YES",
                     "bids": [{"price": "0.4", "size": "1"}], "asks": []})
    st.handle_event({"event_type": "price_change", "price_changes": [
        {"asset_id": "PM-YES", "side": "BUY", "price": "0.41", "size": "2"},
        {"asset_id": "UNKNOWN", "side": "SELL", "price": "0.6", "size": "9"},
    ]})
    assert st.replicas["PM-YES"].bids == {400_000: 1_000_000, 410_000: 2_000_000}


def test_state_tick_size_change_keeps_the_book():
    st = MirrorState([_ref()])
    st.handle_event({"event_type": "book", "asset_id": "PM-YES",
                     "bids": [], "asks": [{"price": "0.6", "size": "1"}]})
    st.handle_event({"event_type": "tick_size_change", "asset_id": "PM-YES"})
    assert st.replicas["PM-YES"].snapshot() is not None


def test_state_marks_a_book_that_empties_but_not_one_that_starts_empty():
    st = MirrorState([_ref()])
    st.handle_event({"event_type": "book", "asset_id": "PM-YES", "bids": [], "asks": []})
    assert st.emptied == set()
    st.handle_event({"event_type": "book", "asset_id": "PM-YES",
                     "bids": [{"price": "0.4", "size": "1"}], "asks": []})
    st.replicas["PM-YES"].mark_stale()
    assert st.emptied == set()
    st.handle_event({"event_type": "book", "asset_id": "PM-YES",
                     "bids": [{"price": "0.4", "size": "1"}], "asks": []})
    st.handle_event({"event_type": "price_change", "price_changes": [
        {"asset_id": "PM-YES", "side": "BUY", "price": "0.4", "size": "0"},
    ]})
    assert st.emptied == {"PM-YES"}
    st.emptied.clear()
    st.handle_event({"event_type": "book", "asset_id": "PM-YES",
                     "bids": [], "asks": [{"price": "0.6", "size": "1"}]})
    st.handle_event({"event_type": "book", "asset_id": "PM-YES", "bids": [], "asks": []})
    assert st.emptied == {"PM-YES"}


def test_state_queues_only_known_asset_trades():
    st = MirrorState([_ref()])
    st.handle_event({"event_type": "last_trade_price", "asset_id": "PM-YES",
                     "price": "0.5", "size": "10", "side": "BUY",
                     "timestamp": "1700000000000"})
    st.handle_event({"event_type": "last_trade_price", "asset_id": "UNKNOWN",
                     "price": "0.5", "size": "10", "side": "BUY",
                     "timestamp": "1700000000000"})
    assert len(st.trades) == 1


class FakeWs:
    """Scripted websocket: yields queued frames, then times out forever."""
    def __init__(self, frames=()):
        self.frames = list(frames)
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)

    async def recv(self):
        if self.frames:
            frame = self.frames.pop(0)
            if isinstance(frame, Exception):
                raise frame
            return frame
        await asyncio.sleep(3600)

    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


def _book(asset):
    return json.dumps([{"event_type": "book", "asset_id": asset,
                        "bids": [{"price": "0.4", "size": "1"}], "asks": []}])


def _fast(monkeypatch, *sockets):
    queue = list(sockets)
    monkeypatch.setattr(feed, "connect", lambda url, **kw: queue.pop(0))
    monkeypatch.setattr(feed, "PING_SECONDS", 0.05)
    monkeypatch.setattr(feed, "RECONNECT_MIN_SECONDS", 0.001)


async def _stop(task):
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def _connected(st, ws, assets, watchdog_seconds=30.0):
    conn = FeedConnection(st, watchdog_seconds)
    await conn.subscribe(assets)
    task = asyncio.create_task(conn.run())
    for _ in range(100):
        if ws.sent:
            break
        await asyncio.sleep(0.005)
    return conn, task


async def test_connection_subscribes_routes_pings_then_watchdog_drops_the_book(monkeypatch):
    st = MirrorState([_ref()])
    ws = FakeWs([_book("PM-YES"), "PONG"])
    _fast(monkeypatch, ws, FakeWs())
    _, task = await _connected(st, ws, ["PM-YES"], watchdog_seconds=0.5)
    await asyncio.sleep(0.2)
    assert json.loads(ws.sent[0]) == {"operation": "subscribe", "assets_ids": ["PM-YES"]}
    assert "PING" in ws.sent[1:]
    assert st.replicas["PM-YES"].snapshot() is not None

    await asyncio.sleep(0.6)
    assert st.replicas["PM-YES"].snapshot() is None
    await _stop(task)


async def test_watchdog_trips_on_wall_clock_despite_chatty_garbage_frames(monkeypatch):
    st = MirrorState([_ref()])
    ws = FakeWs([_book("PM-YES")] + ["PONG"] * 1000)

    async def chatty_recv():
        await asyncio.sleep(0.01)
        return ws.frames.pop(0)

    ws.recv = chatty_recv
    _fast(monkeypatch, ws, FakeWs())
    _, task = await _connected(st, ws, ["PM-YES"], watchdog_seconds=0.3)
    await asyncio.sleep(0.7)
    assert st.replicas["PM-YES"].snapshot() is None
    await _stop(task)


async def test_target_changes_are_in_place_frames_in_chunks(monkeypatch):
    st = MirrorState([_ref(f"A{i}", i) for i in range(5)])
    ws = FakeWs()
    _fast(monkeypatch, ws)
    monkeypatch.setattr(feed, "FRAME_ASSETS", 2)
    conn, task = await _connected(st, ws, [f"A{i}" for i in range(3)])
    await conn.subscribe(["A3", "A4"])
    await conn.unsubscribe(["A0"])
    frames = [json.loads(m) for m in ws.sent]
    assert [(f["operation"], len(f["assets_ids"])) for f in frames[:2]] == [
        ("subscribe", 2), ("subscribe", 1)]
    assert {a for f in frames[:2] for a in f["assets_ids"]} == {"A0", "A1", "A2"}
    assert frames[2:] == [{"operation": "subscribe", "assets_ids": ["A3", "A4"]},
                          {"operation": "unsubscribe", "assets_ids": ["A0"]}]
    assert conn.assets == {"A1", "A2", "A3", "A4"}
    await _stop(task)


async def test_an_asset_with_no_snapshot_is_resubscribed(monkeypatch):
    st = MirrorState([_ref("A", 1), _ref("B", 2)])
    ws = FakeWs([_book("A")])
    _fast(monkeypatch, ws)
    monkeypatch.setattr(feed, "SNAPSHOT_SECONDS", 0.1)
    _, task = await _connected(st, ws, ["A", "B"])
    await asyncio.sleep(0.3)
    frames = [json.loads(m) for m in ws.sent if m != "PING"]
    assert frames[1:3] == [{"operation": "unsubscribe", "assets_ids": ["B"]},
                           {"operation": "subscribe", "assets_ids": ["B"]}]
    assert all("A" not in f["assets_ids"] for f in frames[1:])
    await _stop(task)


async def test_a_normal_close_drops_the_book_until_the_next_snapshot(monkeypatch, caplog):
    st = MirrorState([_ref()])
    closed = ConnectionClosedOK(Close(1000, "all subscribed assets resolved"), None)
    first, second = FakeWs([_book("PM-YES"), closed]), FakeWs()
    _fast(monkeypatch, first, second)
    caplog.set_level(logging.INFO, logger="agentpit.liquidity.feed")
    _, task = await _connected(st, first, ["PM-YES"])
    for _ in range(100):
        if second.sent:
            break
        await asyncio.sleep(0.005)
    assert st.replicas["PM-YES"].snapshot() is None
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    second.frames.append(_book("PM-YES"))
    await asyncio.sleep(0.1)
    assert st.replicas["PM-YES"].snapshot() is not None
    await _stop(task)


async def test_backoff_resets_after_a_session_that_got_a_snapshot(monkeypatch):
    closed = ConnectionClosedOK(Close(1000, "bye"), None)
    snapped = [FakeWs([_book("PM-YES"), closed]) for _ in range(3)]
    _fast(monkeypatch, *snapped, FakeWs([closed]), FakeWs([closed]), FakeWs())
    waits: list[float] = []
    monkeypatch.setattr(feed.random, "uniform", lambda lo, hi: waits.append(hi) or 0.0)
    _, task = await _connected(MirrorState([_ref()]), snapped[0], ["PM-YES"])
    for _ in range(100):
        if len(waits) == 5:
            break
        await asyncio.sleep(0.005)
    assert waits == [0.001, 0.001, 0.001, 0.002, 0.004]
    await _stop(task)


@pytest.mark.asyncio
async def test_a_cancelled_connection_whose_close_fails_stays_cancelled(monkeypatch):
    # Cancelling a connection closes its websocket, and on a dead socket that
    # close raises (websockets' ConnectionClosed) rather than completing. The
    # retry branch caught that, logged it and reconnected, so the cancel was
    # lost: the connection went on writing into books beside the feed that
    # replaced it — every event applied twice.
    connects = []

    class CloseFails:
        async def __aenter__(self):
            connects.append(1)
            if len(connects) > 1:
                # A reconnect after cancel is the regression. End the task
                # here so a regressed build fails the assertion below instead
                # of leaving a task that hangs the whole suite on shutdown.
                raise asyncio.CancelledError
            return self

        async def __aexit__(self, *exc):
            raise ConnectionError("close handshake on a dead socket")

        async def send(self, _msg):
            pass

        async def recv(self):
            await asyncio.Event().wait()

    monkeypatch.setattr(feed, "connect", lambda url, **kw: CloseFails())
    monkeypatch.setattr(feed, "RECONNECT_MIN_SECONDS", 0.001)
    conn = FeedConnection(MirrorState([_ref()]), 30.0)
    await conn.subscribe(["PM-YES"])
    task = asyncio.create_task(conn.run())
    await asyncio.sleep(0.02)
    task.cancel()
    await asyncio.wait({task}, timeout=1.0)
    assert task.done() and task.cancelled()
    assert connects == [1], "a cancelled connection must not reconnect"
