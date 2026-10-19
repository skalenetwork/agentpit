"""The /events listing has a short-TTL response cache so a high client poll
rate collapses to ~one DB read per TTL window.

The cache key is `(limit, offset, category, tag, sorted_subtags)`: once the
listing gained a category filter, keying on `(limit, offset)` alone would serve
one category's page to every other category. Similarly, tag and subtags filters
must be part of the key to avoid serving a filtered page to an unfiltered
request. Subtags are sorted to ensure that `?subtag=a&subtag=b` and
`?subtag=b&subtag=a` (the same OR set) share a single cache entry.
"""

import agentpit.api.routes.events as events
from agentpit.api.routes.events import (
    _EVENTS_CACHE_MAX,
    _EVENTS_CACHE_MAX_LIMIT,
    _EVENTS_TTL_S,
    _list_events_cached,
)


class _FakeService:
    """Counts DB reads; returns a page tagged with the call number."""

    def __init__(self) -> None:
        self.calls = 0

    def list_events_gamma(
        self,
        limit: int,
        offset: int,
        category: str | None = None,
        tag: str | None = None,
        subtags: list[str] | None = None,
        sort=None,
    ):
        self.calls += 1
        return [f"page-{limit}-{offset}-{category}-call{self.calls}"]


def setup_function() -> None:
    events._events_cache.clear()


def test_hit_within_ttl_skips_the_db():
    svc = _FakeService()
    first = _list_events_cached(svc, limit=20, offset=0, category=None, now=100.0)
    again = _list_events_cached(
        svc, limit=20, offset=0, category=None, now=100.0 + _EVENTS_TTL_S - 0.001
    )
    assert svc.calls == 1  # second read served from cache
    assert again == first


def test_refetches_after_ttl_expires():
    svc = _FakeService()
    _list_events_cached(svc, limit=20, offset=0, category=None, now=100.0)
    _list_events_cached(
        svc, limit=20, offset=0, category=None, now=100.0 + _EVENTS_TTL_S + 0.001
    )
    assert svc.calls == 2  # expired -> fresh DB read


def test_distinct_pages_cache_independently():
    svc = _FakeService()
    _list_events_cached(svc, limit=20, offset=0, category=None, now=100.0)
    # next page
    _list_events_cached(svc, limit=20, offset=20, category=None, now=100.0)
    assert svc.calls == 2  # different (limit, offset) -> separate entries


def test_distinct_categories_cache_independently():
    """Same page, different filter: the category MUST be part of the key or
    the Sports page is served to a Crypto request for up to the whole TTL."""
    svc = _FakeService()
    sports = _list_events_cached(
        svc, limit=20, offset=0, category="Sports", now=100.0
    )
    crypto = _list_events_cached(
        svc, limit=20, offset=0, category="Crypto", now=100.0
    )
    assert svc.calls == 2  # different category -> separate entries
    assert sports != crypto


def test_unfiltered_page_is_not_served_to_a_filtered_request():
    svc = _FakeService()
    unfiltered = _list_events_cached(
        svc, limit=20, offset=0, category=None, now=100.0
    )
    filtered = _list_events_cached(
        svc, limit=20, offset=0, category="Sports", now=100.0
    )
    assert svc.calls == 2
    assert unfiltered != filtered


def test_cache_is_bounded_against_junk_categories():
    """`category` is caller-supplied, so the key space is unbounded. A flood of
    distinct categories must not grow the cache without limit."""
    svc = _FakeService()
    for i in range(_EVENTS_CACHE_MAX * 2):
        _list_events_cached(svc, limit=20, offset=0, category=f"junk-{i}", now=100.0)
    assert len(events._events_cache) <= _EVENTS_CACHE_MAX


def test_a_page_larger_than_the_cached_limit_is_never_kept():
    svc = _FakeService()
    for limit in range(_EVENTS_CACHE_MAX_LIMIT + 1, _EVENTS_CACHE_MAX_LIMIT + 121):
        _list_events_cached(svc, limit=limit, offset=0, now=100.0)
    _list_events_cached(svc, limit=_EVENTS_CACHE_MAX_LIMIT + 1, offset=0, now=100.0)
    assert (svc.calls, events._events_cache) == (121, {})


def test_a_full_cache_keeps_the_entries_already_in_it():
    svc = _FakeService()
    _list_events_cached(svc, limit=20, offset=0, now=100.0)
    for i in range(_EVENTS_CACHE_MAX * 2):
        _list_events_cached(svc, limit=20, offset=0, category=f"junk-{i}", now=100.0)
    calls = svc.calls
    _list_events_cached(svc, limit=20, offset=0, now=101.0)
    assert svc.calls == calls


def test_an_insert_drops_every_expired_entry():
    svc = _FakeService()
    _list_events_cached(svc, limit=20, offset=0, category="old", now=100.0)
    _list_events_cached(
        svc, limit=20, offset=0, category="new", now=100.0 + _EVENTS_TTL_S
    )
    assert [key[2] for key in events._events_cache] == ["new"]
