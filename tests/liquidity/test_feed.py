import asyncio
import json

import pytest

from agentpit.liquidity.feed import (
    MarketRef, MirrorState, fetch_books_rest, parse_events, run_connection, shard,
)


def _ref(pm="PM-YES", market_id=1):
    return MarketRef(market_id=market_id, condition_id=f"0xc{market_id}",
                     yes_token=f"y{market_id}", no_token=f"n{market_id}",
                     pm_yes_token=pm)


def test_parse_events_handles_both_framings_and_garbage():
    assert parse_events('{"event_type":"book"}') == [{"event_type": "book"}]
    assert parse_events('[{"a":1},{"b":2}]') == [{"a": 1}, {"b": 2}]
    assert parse_events("PONG") == []
    assert parse_events("[1,2]") == []


def test_shard():
    assert shard(list(range(5)), 2) == [[0, 1], [2, 3], [4]]
    assert shard([], 2) == []


def test_state_routes_book_and_price_change_and_marks_dirty():
    st = MirrorState([_ref()])
    st.handle_event({"event_type": "book", "asset_id": "PM-YES",
                     "bids": [{"price": "0.4", "size": "1"}], "asks": []})
    assert "PM-YES" in st.dirty
    st.dirty.clear()
    st.handle_event({"event_type": "price_change", "price_changes": [
        {"asset_id": "PM-YES", "side": "BUY", "price": "0.41", "size": "2"},
        {"asset_id": "UNKNOWN", "side": "SELL", "price": "0.6", "size": "9"},
    ]})
    assert st.dirty == {"PM-YES"}
    assert st.replicas["PM-YES"].bids == {400_000: 1_000_000, 410_000: 2_000_000}


def test_state_tick_size_change_marks_stale_not_dirty():
    st = MirrorState([_ref()])
    st.handle_event({"event_type": "book", "asset_id": "PM-YES",
                     "bids": [], "asks": [{"price": "0.6", "size": "1"}]})
    st.dirty.clear()
    st.handle_event({"event_type": "tick_size_change", "asset_id": "PM-YES"})
    assert st.replicas["PM-YES"].stale and st.dirty == set()


def test_state_queues_only_known_asset_trades():
    st = MirrorState([_ref()])
    st.handle_event({"event_type": "last_trade_price", "asset_id": "PM-YES",
                     "price": "0.5", "size": "10", "side": "BUY",
                     "timestamp": "1700000000000"})
    st.handle_event({"event_type": "last_trade_price", "asset_id": "UNKNOWN",
                     "price": "0.5", "size": "10", "side": "BUY",
                     "timestamp": "1700000000000"})
    assert len(st.trades) == 1


def test_fetch_books_rest_batches_and_applies():
    calls = []

    class FakeResp:
        def raise_for_status(self): pass
        def json(self):
            return [{"asset_id": tid["token_id"], "bids": [], "asks": []}
                    for tid in calls[-1]]

    class FakeClient:
        def post(self, url, json):
            calls.append(json)
            return FakeResp()

    ids = [f"a{i}" for i in range(250)]
    books = fetch_books_rest(ids, client=FakeClient(), batch_size=100)
    assert [len(c) for c in calls] == [100, 100, 50]
    assert len(books) == 250


class FakeWs:
    """Scripted websocket: yields queued frames, then times out forever."""
    def __init__(self, frames):
        self.frames = list(frames)
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)

    async def recv(self):
        if self.frames:
            return self.frames.pop(0)
        await asyncio.sleep(3600)

    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


@pytest.mark.asyncio
async def test_run_connection_subscribes_routes_pings_then_watchdog_stales():
    ref = _ref()
    st = MirrorState([ref])
    book = json.dumps([{"event_type": "book", "asset_id": "PM-YES",
                        "bids": [{"price": "0.4", "size": "1"}], "asks": []}])
    ws = FakeWs([book, "PONG"])

    def connect(url):
        return ws

    task = asyncio.create_task(run_connection(
        st, ["PM-YES"], connect=connect,
        ping_interval=0.05, watchdog_seconds=0.5, reconnect_delay=10.0))
    await asyncio.sleep(0.2)
    # Phase 1: subscribed, book routed, PING sent on idle — before the watchdog.
    sub = json.loads(ws.sent[0])
    assert sub == {"assets_ids": ["PM-YES"], "type": "market"}
    assert "PING" in ws.sent[1:]                  # keepalive sent on idle
    assert st.replicas["PM-YES"].seeded

    await asyncio.sleep(0.6)
    # Phase 2: 0.5s with no events → watchdog tripped → replica marked stale
    # (it re-seeds from the fresh snapshot a real reconnect delivers).
    assert st.replicas["PM-YES"].stale

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


class ChattyWs:
    """Hostile server: sprays PONG frames faster than ping_interval forever."""
    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)

    async def recv(self):
        await asyncio.sleep(0.02)
        return "PONG"

    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


@pytest.mark.asyncio
async def test_watchdog_trips_on_wall_clock_despite_chatty_garbage_frames():
    ref = _ref()
    st = MirrorState([ref])
    st.handle_event({"event_type": "book", "asset_id": "PM-YES",
                     "bids": [{"price": "0.4", "size": "1"}], "asks": []})
    ws = ChattyWs()

    task = asyncio.create_task(run_connection(
        st, ["PM-YES"], connect=lambda url: ws,
        ping_interval=0.05, watchdog_seconds=0.3, reconnect_delay=10.0))
    await asyncio.sleep(0.7)
    # No real EVENTS for 0.3s of wall-clock → watchdog must trip even though
    # garbage frames keep every recv() from timing out.
    assert st.replicas["PM-YES"].stale

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_a_cancelled_connection_whose_close_fails_stays_cancelled():
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

    task = asyncio.create_task(run_connection(
        MirrorState([_ref()]), ["PM-YES"], connect=lambda url: CloseFails(),
        ping_interval=5.0, watchdog_seconds=30.0, reconnect_delay=0.001))
    await asyncio.sleep(0.02)
    task.cancel()
    await asyncio.wait({task}, timeout=1.0)
    assert task.done() and task.cancelled()
    assert connects == [1], "a cancelled connection must not reconnect"
