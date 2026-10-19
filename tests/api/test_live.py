import asyncio
from collections import Counter

from fastapi.testclient import TestClient

from agentpit.api.main import app
from agentpit.api.routes import live
from agentpit.liquidity import feed
from tests.api.test_markets_board import _event, _market
from tests.db_helpers import fresh_test_conn


def test_a_stream_sends_a_snapshot_then_only_changes(house_book, monkeypatch):
    conn = fresh_test_conn()
    a = _market(
        house_book,
        conn,
        _event(conn, "live-a", "A?", "Crypto"),
        "live-a",
        "A?",
        book=(400_000, 420_000),
    )
    b = _market(
        house_book,
        conn,
        _event(conn, "live-b", "B?", "Crypto"),
        "live-b",
        "B?",
        book=(600_000, 610_000),
    )
    conn.close()
    monkeypatch.setattr(live, "TICK", 0.01)

    async def stream():
        runner = asyncio.create_task(live.run())
        await asyncio.sleep(0)
        events = live.live(f"{a.market_id},{b.market_id}")
        snapshot = await anext(events)
        house_book(a.erc1155_tokens[0][0], bids=(("0.41", "1"),), asks=(("0.43", "1"),))
        change = await anext(events)
        house_book(b.erc1155_tokens[0][0])
        dropped = await anext(events)
        await events.aclose()
        runner.cancel()
        return snapshot, change, dropped, dict(live._watched)

    snapshot, change, dropped, watched = asyncio.run(stream())
    assert (snapshot.data, snapshot.retry) == (
        {a.market_id: (400, 420), b.market_id: (600, 610)},
        5000,
    )
    assert (change.data, dropped.data, watched) == (
        {a.market_id: (410, 430)},
        {b.market_id: (None, None)},
        {},
    )


def test_a_stream_takes_at_most_400_markets():
    with TestClient(app) as client:
        response = client.get("/live", params={"m": ",".join(map(str, range(401)))})
    assert response.status_code == 422


class _Unwalkable(dict):
    """A by_asset that fails the test if anything walks it."""

    def _walk(self, *_):
        raise AssertionError("walked every mirrored market")

    __iter__ = keys = values = items = _walk


def test_a_read_looks_up_only_the_markets_it_is_asked_for(house_book):
    conn = fresh_test_conn()
    a = _market(
        house_book,
        conn,
        _event(conn, "live-c", "C?", "Crypto"),
        "live-c",
        "C?",
        book=(400_000, 420_000),
    )
    conn.close()
    state = feed.HOUSE.state
    state.by_asset = _Unwalkable(state.by_asset)
    assert live._read({a.market_id, 999_999}) == {a.market_id: (400, 420)}


class _NoRescan(Counter):
    def _keep_positive(self):
        raise AssertionError("a disconnect rescanned every watched id")


def test_a_disconnect_touches_only_its_own_ids(monkeypatch):
    others = {i: 1 for i in range(1000, 1500)}
    watched = _NoRescan(others)
    monkeypatch.setattr(live, "_watched", watched)
    monkeypatch.setattr(live, "_tick", None)

    async def stream():
        events = live.live("1,2,1000")
        await anext(events)
        await events.aclose()

    asyncio.run(stream())
    assert dict(watched) == others


def test_a_stream_ends_once_its_lifetime_is_up(monkeypatch):
    monkeypatch.setattr(live, "TICK", 0.01)
    monkeypatch.setattr(live, "MAX_STREAM_SECONDS", 0.05)

    async def stream():
        runner = asyncio.create_task(live.run())
        await asyncio.sleep(0)
        events = live.live("1")
        await anext(events)
        rest = [event async for event in events]
        runner.cancel()
        return rest, dict(live._watched)

    assert asyncio.run(asyncio.wait_for(stream(), 5)) == ([], {})


def _get(m="1"):
    return TestClient(app).get("/live", params={"m": m})


def test_a_stream_holds_its_slot_until_it_ends(monkeypatch):
    seen = []
    monkeypatch.setattr(live, "_tick", None)
    monkeypatch.setattr(
        live, "_read", lambda ids: seen.append(dict(live._streams)) or {}
    )
    response = _get()
    assert (response.status_code, seen, dict(live._streams)) == (
        200,
        [{"testclient": 1}],
        {},
    )


def test_a_freed_slot_takes_the_next_stream(monkeypatch):
    monkeypatch.setattr(live, "_tick", None)
    live._streams["testclient"] = live.MAX_STREAMS_PER_CLIENT - 1
    assert [_get().status_code for _ in range(3)] == [200, 200, 200]
    assert dict(live._streams) == {"testclient": live.MAX_STREAMS_PER_CLIENT - 1}


def test_an_address_at_its_cap_gets_429(monkeypatch):
    monkeypatch.setattr(live, "_tick", None)
    live._streams["testclient"] = live.MAX_STREAMS_PER_CLIENT
    response = _get()
    assert (response.status_code, dict(live._streams), dict(live._watched)) == (
        429,
        {"testclient": live.MAX_STREAMS_PER_CLIENT},
        {},
    )


def test_a_full_server_gets_503(monkeypatch):
    monkeypatch.setattr(live, "_tick", None)
    live._streams["elsewhere"] = live.MAX_STREAMS
    response = _get()
    assert (response.status_code, dict(live._streams), dict(live._watched)) == (
        503,
        {"elsewhere": live.MAX_STREAMS},
        {},
    )


def test_a_malformed_request_leaves_no_slot_behind():
    assert (_get("x").status_code, dict(live._streams)) == (422, {})
