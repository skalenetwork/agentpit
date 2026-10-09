"""The sort enum is the only place a caller-supplied string becomes SQL."""

from __future__ import annotations

import pytest

from agentpit.datastructures.event_sort import EventSort


def test_wire_values_match_what_the_ui_sends():
    assert [s.value for s in EventSort] == [
        "volume24h",
        "totalVolume",
        "liquidity",
        "competitive",
        "newest",
        "endingSoon",
    ]


def test_the_default_is_24h_volume():
    # The home page has ranked on this since before sorting existed; changing
    # it would silently reorder everyone's first screen.
    assert EventSort.DEFAULT is EventSort.VOLUME_24H


def test_parse_accepts_a_known_wire_value():
    assert EventSort.parse("liquidity") is EventSort.LIQUIDITY
    assert EventSort.parse("endingSoon") is EventSort.ENDING_SOON


def test_parse_falls_back_rather_than_raising():
    """`sort` is caller-supplied. A 500 on `?sort=nonsense` would let anyone
    take the listing down, and an exception here would reach the home page."""
    for junk in ["nonsense", "", "  ", None, 7, "DROP TABLE events"]:
        assert EventSort.parse(junk) is EventSort.DEFAULT


def test_parse_is_case_and_whitespace_tolerant():
    assert EventSort.parse(" Liquidity ") is EventSort.LIQUIDITY
    assert EventSort.parse("ENDINGSOON") is EventSort.ENDING_SOON


def test_the_default_order_by_is_unchanged():
    """Pinned verbatim: two existing tests depend on this exact clause."""
    assert (
        EventSort.VOLUME_24H.order_by()
        == "VOLUME_24HR DESC NULLS LAST, EVENT_ID DESC"
    )


def test_every_order_by_ends_with_the_unique_tiebreak():
    """Without it, two events with equal sort values can swap between LIMIT
    pages — one gets shown twice and another never at all."""
    for sort in EventSort:
        assert sort.order_by().endswith("EVENT_ID DESC"), sort


def test_every_order_by_puts_missing_values_last():
    """An event we never captured a figure for belongs at the bottom, not at
    the top of "Ending Soon"."""
    for sort in EventSort:
        head = sort.order_by().split(",")[0]
        assert "NULLS LAST" in head, sort


def test_ending_soon_is_the_only_ascending_sort():
    assert EventSort.ENDING_SOON.order_by().startswith("END_DATE ASC")
    for sort in EventSort:
        if sort is not EventSort.ENDING_SOON:
            assert " ASC" not in sort.order_by().split(",")[0], sort


def test_each_sort_uses_its_own_column():
    columns = {sort: sort.order_by().split()[0] for sort in EventSort}
    assert columns[EventSort.VOLUME_24H] == "VOLUME_24HR"
    assert columns[EventSort.TOTAL_VOLUME] == "VOLUME"
    assert columns[EventSort.LIQUIDITY] == "LIQUIDITY"
    assert columns[EventSort.COMPETITIVE] == "COMPETITIVE"
    assert columns[EventSort.NEWEST] == "START_DATE"
    assert columns[EventSort.ENDING_SOON] == "END_DATE"
    # Liquidity and Competitive were the same sort before this existed.
    assert len(set(columns.values())) == len(EventSort)


def test_order_by_contains_no_caller_supplied_text():
    """The enum is the whole allow-list. Nothing a caller sends reaches SQL."""
    assert EventSort.parse("VOLUME_24HR; DROP TABLE events").order_by() == (
        EventSort.DEFAULT.order_by()
    )


@pytest.mark.parametrize("sort", list(EventSort))
def test_order_by_is_a_bare_clause(sort: EventSort):
    """Callers splice this after the words ORDER BY; a leading keyword or a
    trailing semicolon would break the query they build."""
    clause = sort.order_by()
    assert not clause.upper().startswith("ORDER BY")
    assert ";" not in clause


# ----- capturing the two upstream metrics -------------------------------------

from typing import Any  # noqa: E402

from agentpit.db.table_read import TableRead  # noqa: E402
from agentpit.db.table_write import TableWrite  # noqa: E402
from agentpit.polymarket.polymarket_sync import _extract_event_metadata  # noqa: E402
from tests.db_helpers import fresh_test_conn  # noqa: E402


@pytest.fixture()
def db() -> Any:
    conn = fresh_test_conn()
    yield conn
    conn.close()


def test_a_fresh_event_has_no_metrics_yet(db):
    event = TableWrite.upsert_event(db, slug="m1", title="M1")
    stored = TableRead.get_event_by_id(db, event.event_id)
    assert stored is not None
    assert stored.liquidity is None
    assert stored.competitive is None


def test_update_event_metrics_stores_both(db):
    event = TableWrite.upsert_event(db, slug="m2", title="M2")
    TableWrite.update_event_metrics(db, event.event_id, 1976643.77, 0.9846)
    stored = TableRead.get_event_by_id(db, event.event_id)
    assert stored is not None
    assert abs(stored.liquidity - 1976643.77) < 1e-6
    assert abs(stored.competitive - 0.9846) < 1e-9


def test_update_event_metrics_skips_each_figure_independently(db):
    """A degraded pass carrying only one figure must not blank the other —
    the same discipline update_event_volume already follows."""
    event = TableWrite.upsert_event(db, slug="m3", title="M3")
    TableWrite.update_event_metrics(db, event.event_id, 500.0, 0.5)
    TableWrite.update_event_metrics(db, event.event_id, 900.0, None)
    stored = TableRead.get_event_by_id(db, event.event_id)
    assert stored is not None
    assert abs(stored.liquidity - 900.0) < 1e-6
    assert abs(stored.competitive - 0.5) < 1e-9, "competitive was clobbered"


def test_update_event_metrics_with_both_none_is_a_no_op(db):
    event = TableWrite.upsert_event(db, slug="m4", title="M4")
    TableWrite.update_event_metrics(db, event.event_id, 7.0, 0.7)
    TableWrite.update_event_metrics(db, event.event_id, None, None)
    stored = TableRead.get_event_by_id(db, event.event_id)
    assert stored is not None
    assert abs(stored.liquidity - 7.0) < 1e-6


def _pm_market(**event_fields) -> dict:
    """An upstream market payload wrapping one event."""
    return {
        "tags": [],
        "events": [
            {"id": "9", "slug": "e", "title": "E", **event_fields},
        ],
    }


def test_extract_event_metadata_reads_both_metrics():
    meta = _extract_event_metadata(
        _pm_market(liquidity=1976643.77453, competitive=0.9846153846153846)
    )
    assert meta is not None
    assert abs(meta.liquidity - 1976643.77453) < 1e-6
    assert abs(meta.competitive - 0.9846153846153846) < 1e-12


def test_extract_event_metadata_reads_them_from_strings():
    """Gamma stringifies numbers on some payloads — volume already arrives
    that way, so the same coercion has to cover these."""
    meta = _extract_event_metadata(_pm_market(liquidity="123.5", competitive="0.42"))
    assert meta is not None
    assert abs(meta.liquidity - 123.5) < 1e-6
    assert abs(meta.competitive - 0.42) < 1e-9


def test_extract_event_metadata_leaves_absent_metrics_none():
    """None means "upstream said nothing, keep what is stored" — the same
    signal update_event_metrics acts on."""
    meta = _extract_event_metadata(_pm_market())
    assert meta is not None
    assert meta.liquidity is None
    assert meta.competitive is None


def test_extract_event_metadata_survives_unparseable_metrics():
    """A raise here would permanently skip this market on every future pass."""
    meta = _extract_event_metadata(_pm_market(liquidity="n/a", competitive={}))
    assert meta is not None
    assert meta.liquidity is None
    assert meta.competitive is None


# ----- ordering the listing ---------------------------------------------------

import time  # noqa: E402

from agentpit.datastructures.condition_id import ConditionId  # noqa: E402
from agentpit.datastructures.create_market_request import CreateMarketRequest  # noqa: E402
from agentpit.datastructures.market_state import MarketState  # noqa: E402


def _make_market(
    db,
    *,
    question: str,
    cond_id: str,
    event_id: int | None = None,
    outcome_label: str | None = None,
    icon_url: str | None = None,
):
    """Insert a market via the write path. Returns the created Market."""
    request = CreateMarketRequest(
        question=question,
        description=f"desc for {question}",
        erc1155_tokens=[(f"{cond_id}-yes", "Yes"), (f"{cond_id}-no", "No")],
        slug=question.lower().replace(" ", "-").replace("?", ""),
        condition_id=ConditionId(cond_id),
        state=MarketState.ACTIVE,
        event_id=event_id,
        outcome_label=outcome_label,
        icon_url=icon_url,
    )
    return TableWrite.create_market(db, request, is_polygon_market=False)


def _hex32(seed: str) -> str:
    """Return a 32-byte hex condition_id derived from a short label."""
    payload = seed.encode().hex().ljust(64, "0")[:64]
    return "0x" + payload


def _event(db, slug, **cols):
    """An event with whatever metrics the test cares about."""
    ev = TableWrite.upsert_event(
        db,
        slug=slug,
        title=slug.upper(),
        start_date=cols.pop("start_date", None),
        end_date=cols.pop("end_date", None),
    )
    if "volume_24hr" in cols or "volume" in cols:
        TableWrite.update_event_volume(
            db, ev.event_id, cols.pop("volume_24hr", None), cols.pop("volume", None)
        )
    if "liquidity" in cols or "competitive" in cols:
        TableWrite.update_event_metrics(
            db, ev.event_id, cols.pop("liquidity", None), cols.pop("competitive", None)
        )
    assert not cols, f"unused: {cols}"
    return ev


def _slugs(db, sort):
    pairs, _total = TableRead.list_events_with_markets(db, limit=50, offset=0, sort=sort)
    return [ev.slug for ev, _ in pairs]


def test_default_sort_is_unchanged_when_none_is_passed(db):
    _event(db, "lo", volume_24hr=1.0)
    _event(db, "hi", volume_24hr=99.0)
    assert _slugs(db, None) == ["hi", "lo"]


def test_total_volume_ranks_on_the_all_time_figure(db):
    # Deliberately opposed to the 24h figure, so a sort that ignored the
    # parameter would fail rather than coincidentally pass.
    _event(db, "a", volume_24hr=100.0, volume=1.0)
    _event(db, "b", volume_24hr=1.0, volume=100.0)
    assert _slugs(db, EventSort.TOTAL_VOLUME) == ["b", "a"]
    assert _slugs(db, EventSort.VOLUME_24H) == ["a", "b"]


def test_liquidity_and_competitive_are_different_sorts(db):
    """They ranked identically before this change, because both were computed
    from the number of outcomes."""
    _event(db, "deep", liquidity=1_000_000.0, competitive=0.10)
    _event(db, "contested", liquidity=100.0, competitive=0.99)
    assert _slugs(db, EventSort.LIQUIDITY) == ["deep", "contested"]
    assert _slugs(db, EventSort.COMPETITIVE) == ["contested", "deep"]


def test_newest_ranks_on_start_date_descending(db):
    _event(db, "old", start_date=1_000)
    _event(db, "new", start_date=9_000)
    assert _slugs(db, EventSort.NEWEST) == ["new", "old"]


def test_ending_soon_ranks_on_end_date_ascending(db):
    # Both in the future, or the new "not already ended" restriction (see
    # below) would drop them rather than order them.
    now = int(time.time())
    _event(db, "later", end_date=now + 9_000)
    _event(db, "sooner", end_date=now + 1_000)
    assert _slugs(db, EventSort.ENDING_SOON) == ["sooner", "later"]


def test_missing_values_sort_last_even_when_ascending(db):
    """The trap: NULL sorts FIRST by default under ASC in Postgres, which
    would put every never-captured event at the top of "Ending Soon"."""
    now = int(time.time())
    _event(db, "dated", end_date=now + 5_000)
    _event(db, "undated")
    assert _slugs(db, EventSort.ENDING_SOON) == ["dated", "undated"]


def test_missing_values_sort_last_when_descending(db):
    _event(db, "measured", liquidity=5.0)
    _event(db, "unmeasured")
    assert _slugs(db, EventSort.LIQUIDITY) == ["measured", "unmeasured"]


def test_ties_break_on_event_id_so_pages_do_not_overlap(db):
    """Equal sort values with no unique tiebreak let two events swap between
    LIMIT/OFFSET pages — one shown twice, the other never."""
    first = _event(db, "t1", liquidity=42.0)
    second = _event(db, "t2", liquidity=42.0)
    assert _slugs(db, EventSort.LIQUIDITY) == ["t2", "t1"]
    assert second.event_id > first.event_id

    page1, _ = TableRead.list_events_with_markets(
        db, limit=1, offset=0, sort=EventSort.LIQUIDITY
    )
    page2, _ = TableRead.list_events_with_markets(
        db, limit=1, offset=1, sort=EventSort.LIQUIDITY
    )
    assert [e.slug for e, _ in page1] + [e.slug for e, _ in page2] == ["t2", "t1"]


def test_sort_composes_with_a_tag_filter(db):
    """Filtering and ordering are independent: the sort must apply to the
    filtered set, not to the whole table."""
    keep = _event(db, "keep", liquidity=1.0)
    _event(db, "drop", liquidity=999.0)
    market = _make_market(db, question="q1?", cond_id=_hex32("s1"), event_id=keep.event_id)
    TableWrite.replace_market_tags(db, market_id=market.market_id, tags=[("politics", "Politics")])

    pairs, total = TableRead.list_events_with_markets(
        db, limit=50, offset=0, tag="politics", sort=EventSort.LIQUIDITY
    )
    assert [ev.slug for ev, _ in pairs] == ["keep"]
    assert total == 1


# ----- "Ending Soon" excludes already-ended events -----------------------------


def test_ending_soon_excludes_events_already_ended(db):
    """The regression this task fixes: ASC over the whole catalogue put
    months-old events first. Under ENDING_SOON, a past END_DATE must be
    dropped rather than lead the page."""
    now = int(time.time())
    _event(db, "past", end_date=now - 3600)
    _event(db, "future", end_date=now + 3600)
    assert _slugs(db, EventSort.ENDING_SOON) == ["future"]


def test_ending_soon_total_matches_the_filtered_count(db):
    """`total` shares the same `where` as the page, so it must reflect the
    exclusion too, not the whole table."""
    now = int(time.time())
    _event(db, "past", end_date=now - 3600)
    _event(db, "future", end_date=now + 3600)
    pairs, total = TableRead.list_events_with_markets(
        db, limit=50, offset=0, sort=EventSort.ENDING_SOON
    )
    assert [ev.slug for ev, _ in pairs] == ["future"]
    assert total == 1


@pytest.mark.parametrize(
    "sort", [s for s in EventSort if s is not EventSort.ENDING_SOON]
)
def test_only_ending_soon_excludes_past_dated_events(db, sort):
    """Every other sort must keep showing a past-dated event — the
    restriction is scoped to ENDING_SOON alone."""
    now = int(time.time())
    _event(db, "past", end_date=now - 3600)
    assert "past" in _slugs(db, sort)


def test_ending_soon_composes_with_a_tag_filter(db):
    now = int(time.time())
    keep = _event(db, "keep", end_date=now + 3600)
    _event(db, "past", end_date=now - 3600)
    drop = _event(db, "drop", end_date=now + 7200)
    market = _make_market(db, question="q2?", cond_id=_hex32("s2"), event_id=keep.event_id)
    TableWrite.replace_market_tags(db, market_id=market.market_id, tags=[("politics", "Politics")])
    _ = drop  # untagged, and future — excluded by the tag filter, not the date filter

    pairs, total = TableRead.list_events_with_markets(
        db, limit=50, offset=0, tag="politics", sort=EventSort.ENDING_SOON
    )
    assert [ev.slug for ev, _ in pairs] == ["keep"]
    assert total == 1


def test_ending_soon_composes_with_a_category_filter(db):
    now = int(time.time())
    keep = TableWrite.upsert_event(
        db, slug="keep-cat", title="KEEP", end_date=now + 3600, category="Sports"
    )
    TableWrite.upsert_event(
        db, slug="past-cat", title="PAST", end_date=now - 3600, category="Sports"
    )
    TableWrite.upsert_event(
        db, slug="other-cat", title="OTHER", end_date=now + 3600, category="Politics"
    )

    pairs, total = TableRead.list_events_with_markets(
        db, limit=50, offset=0, category="Sports", sort=EventSort.ENDING_SOON
    )
    assert [ev.slug for ev, _ in pairs] == [keep.slug]
    assert total == 1
