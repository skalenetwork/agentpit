import asyncio
from collections import Counter
from collections.abc import AsyncIterator, Iterable
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.sse import EventSourceResponse, ServerSentEvent

from agentpit.liquidity import feed

TICK = 5
Tops = dict[int, tuple[int | None, int | None]]

router = APIRouter(tags=["markets"])
_watched: Counter[int] = Counter()
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


async def run() -> None:
    global _tops, _tick
    while True:
        tick = _tick = asyncio.Event()
        await asyncio.sleep(TICK)
        _tops = _read(set(_watched))
        tick.set()


@router.get("/live", response_class=EventSourceResponse)
async def live(
    m: Annotated[str, Query(pattern=r"^[0-9]{1,12}(,[0-9]{1,12}){0,399}$")],
) -> AsyncIterator[ServerSentEvent]:
    ids = {int(i) for i in m.split(",")}
    _watched.update(ids)
    try:
        sent = _read(ids)
        yield ServerSentEvent(data=sent, retry=5000)
        while _tick is not None:
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
