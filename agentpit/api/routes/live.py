import asyncio
from collections import Counter
from collections.abc import AsyncIterator
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


def _read(ids: set[int]) -> Tops:
    house = feed.HOUSE
    if house is None:
        return {}
    tokens = {
        r.market_id: r.yes_token
        for r in tuple(house.state.by_asset.values())
        if r.market_id in ids
    }
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
    global _watched
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
        _watched -= Counter(ids)
