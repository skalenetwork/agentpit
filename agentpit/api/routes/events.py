import threading
import time
from typing import Annotated

from fastapi import APIRouter, Query

from agentpit.api.deps import EventServiceDep
from agentpit.datastructures.event_sort import EventSort
from agentpit.datastructures.event_with_markets import ListEventCategoriesResponse
from agentpit.datastructures.gamma_market import GammaEvent

router = APIRouter(tags=["events"])

# Short-TTL response cache for the event listing. The home page polls /events
# every few seconds; without a cache, N clients = N DB reads per interval even
# though the list only changes when a sync runs. With it, a poll burst collapses
# to ~one DB read per TTL window, regardless of client count. Keyed by
# (limit, offset, category, tag, subtags, sort) — every filter MUST be part of
# the key or a filtered/sorted page would be served to a differently
# filtered/sorted request (and vice versa) for up to one TTL. Per-process;
# staleness is bounded by the TTL.
_EVENTS_TTL_S = 3.0
# The key space is caller-supplied (any limit, offset or filter), so the cache
# is bounded by count AND page size: only pages of at most
# _EVENTS_CACHE_MAX_LIMIT events are kept (the app asks for 20), so it holds at
# most _EVENTS_CACHE_MAX * _EVENTS_CACHE_MAX_LIMIT events. Every insert first
# drops the entries past their TTL, and a cache still full skips the insert
# instead of flushing the entries other callers are polling.
_EVENTS_CACHE_MAX = 32
_EVENTS_CACHE_MAX_LIMIT = 100
_events_cache_lock = threading.Lock()
_events_cache: dict[
    tuple[int, int, str | None, str | None, tuple[str, ...], str],
    tuple[float, list[GammaEvent]],
] = {}


def _list_events_cached(
    service,
    *,
    limit: int,
    offset: int,
    category: str | None = None,
    tag: str | None = None,
    subtags: list[str] | None = None,
    sort: str | None = None,
    now: float,
) -> list[GammaEvent]:
    """Return the cached page if it is younger than the TTL, else fetch + store.

    `now` is injected (monotonic seconds) so the TTL is deterministically
    testable. The sync `def` route runs in FastAPI's threadpool, and an insert
    walks the dict to drop expired entries, so every change happens under
    `_events_cache_lock`; a rare concurrent miss just does one extra harmless
    DB read.

    EVERY filter must be part of the key. A key missing `tag` would serve a
    filtered page to an unfiltered request (and the reverse) for up to one TTL.
    The subtag list is sorted into a tuple so `?subtag=a&subtag=b` and
    `?subtag=b&subtag=a` — the same OR set — share one entry.
    """
    normalized = category.strip() if category else None
    normalized_tag = tag.strip().lower() if tag else None
    normalized_subtags = tuple(
        sorted(s.strip().lower() for s in (subtags or []) if s and s.strip())
    )
    resolved_sort = EventSort.parse(sort)
    key = (
        limit,
        offset,
        normalized or None,
        normalized_tag,
        normalized_subtags,
        # The resolved member, not the raw string: `?sort=nonsense` and no
        # `sort` at all produce the same page, so they must share one entry
        # rather than filling the cache with junk keys.
        resolved_sort.value,
    )
    hit = _events_cache.get(key)
    if hit is not None and now - hit[0] < _EVENTS_TTL_S:
        return hit[1]
    result = service.list_events_gamma(
        limit=limit,
        offset=offset,
        category=normalized,
        tag=normalized_tag,
        subtags=list(normalized_subtags),
        sort=resolved_sort,
    )
    if limit > _EVENTS_CACHE_MAX_LIMIT:
        return result
    with _events_cache_lock:
        for k in [k for k, (at, _) in _events_cache.items() if now - at >= _EVENTS_TTL_S]:
            del _events_cache[k]
        if key in _events_cache or len(_events_cache) < _EVENTS_CACHE_MAX:
            _events_cache[key] = (now, result)
    return result


@router.get("/events", response_model=list[GammaEvent])
def list_events(
    service: EventServiceDep,
    limit: int = 100,
    offset: int = 0,
    category: str | None = None,
    tag: str | None = None,
    subtag: Annotated[list[str] | None, Query()] = None,
    sort: str | None = None,
) -> list[GammaEvent]:
    return _list_events_cached(
        service,
        limit=limit,
        offset=offset,
        category=category,
        tag=tag,
        subtags=subtag,
        sort=sort,
        now=time.monotonic(),
    )


# NOTE: declaration order is load-bearing. FastAPI matches routes in the order
# they are registered, so /events/categories MUST stay above /events/{slug} —
# otherwise it resolves as get_event(slug="categories") and 404s.
@router.get("/events/categories", response_model=ListEventCategoriesResponse)
def list_event_categories(service: EventServiceDep) -> ListEventCategoriesResponse:
    return service.list_categories()


@router.get("/events/{slug}", response_model=GammaEvent)
def get_event(slug: str, service: EventServiceDep) -> GammaEvent:
    return service.get_event_gamma(slug)
