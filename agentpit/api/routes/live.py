import asyncio
from collections import Counter
from collections.abc import AsyncIterator, Iterable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent

from agentpit.liquidity import feed

TICK = 5
# A stream ends after this long. The browser's EventSource reconnects on its
# own after the `retry` the first event sets, so a viewer sees only a short
# pause, and no stream keeps its slot (or its ids in `_watched`) forever.
MAX_STREAM_SECONDS = 600
# Every open stream wakes on every tick and diffs its ids on the event loop
# the mirror feed runs on, so the number of open streams is what bounds that
# work (and the sockets it holds). The landing page opens one stream per
# visible tab; past the cap it gets an error and retries on its own.
MAX_STREAMS = 500
MAX_STREAMS_PER_CLIENT = 10
Tops = dict[int, tuple[int | None, int | None]]

router = APIRouter(tags=["markets"])
_watched: Counter[int] = Counter()
_streams: Counter[str] = Counter()  # open streams per client address
_tops: Tops = {}
_tick: asyncio.Event | None = None


def _mills(price: int | None) -> int | None:
    return None if price is None else price // 1000


def _drop(counter: Counter, keys: Iterable) -> None:
    """Take one off each key and delete it at zero.

    Not `counter -= Counter(keys)`: that also rescans every key in the counter
    to drop the non-positive ones, so each disconnect would cost as much as
    the ids every other open stream watches.
    """
    for k in keys:
        if (n := counter[k] - 1) > 0:
            counter[k] = n
        else:
            del counter[k]


def _read(ids: set[int]) -> Tops:
    house = feed.HOUSE
    if house is None:
        return {}
    # A lookup per requested id, not a walk over every mirrored market: this
    # runs on the event loop on every connect and every tick.
    by_market = house.state.by_market
    tokens = {m: r.yes_token for m in ids if (r := by_market.get(m)) is not None}
    tops = feed.tops(list(tokens.values()))
    return {
        m: (_mills(tops[t][0]), _mills(tops[t][1]))
        for m, t in tokens.items()
        if t in tops
    }


async def _admit(request: Request) -> AsyncIterator[None]:
    """Hold a stream slot for the caller's address while its stream is open.

    A dependency, because by the time the stream body runs the 200 has been
    chosen and a refusal is no longer possible. FastAPI exits a yield
    dependency only once the response has finished, so the slot is held for
    the whole stream and given back however the stream ends. The address is
    the real caller because uvicorn runs with `--proxy-headers` (see
    deploy/Dockerfile.api).
    """
    client = request.client.host if request.client else ""
    if _streams.total() >= MAX_STREAMS:
        raise HTTPException(503, "too many live streams are open, try again later")
    if _streams[client] >= MAX_STREAMS_PER_CLIENT:
        raise HTTPException(
            429, f"at most {MAX_STREAMS_PER_CLIENT} live streams per address"
        )
    _streams[client] += 1
    try:
        yield
    finally:
        _drop(_streams, (client,))


async def run() -> None:
    global _tops, _tick
    while True:
        tick = _tick = asyncio.Event()
        await asyncio.sleep(TICK)
        _tops = _read(set(_watched))
        tick.set()


@router.get(
    "/live", response_class=EventSourceResponse, dependencies=[Depends(_admit)]
)
async def live(
    m: Annotated[str, Query(pattern=r"^[0-9]{1,12}(,[0-9]{1,12}){0,399}$")],
) -> AsyncIterator[ServerSentEvent]:
    ids = {int(i) for i in m.split(",")}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + MAX_STREAM_SECONDS
    _watched.update(ids)
    try:
        sent = _read(ids)
        yield ServerSentEvent(data=sent, retry=5000)
        while _tick is not None and loop.time() < deadline:
            await _tick.wait()
            changed = {
                k: v
                for k in ids
                if (v := _tops.get(k, (None, None))) != sent.get(k, (None, None))
            }
            if changed:
                sent = sent | changed
                yield ServerSentEvent(data=changed)
    finally:
        _drop(_watched, ids)
