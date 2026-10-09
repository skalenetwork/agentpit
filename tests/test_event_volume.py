"""Upstream 24h-volume capture, ordering, and serialization.

Covers the homepage-by-volume feature: events store an upstream `VOLUME_24HR`,
`list_events_with_markets` ranks by it (NULLS LAST), the sync extractor surfaces
it, and the Gamma serializer emits it.
"""

from agentpit.datastructures.event import Event
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.polymarket.gamma import to_gamma_event
from agentpit.polymarket.polymarket_sync import _extract_event_metadata
from tests.db_helpers import fresh_test_conn


# ----- ordering ---------------------------------------------------------------


def test_events_ordered_by_volume_desc_nulls_last():
    conn = fresh_test_conn()
    a = TableWrite.upsert_event(conn, slug="a", title="A")
    TableWrite.upsert_event(conn, slug="b", title="B")  # stays NULL volume
    c = TableWrite.upsert_event(conn, slug="c", title="C")
    TableWrite.update_event_volume(conn, a.event_id, 100.0)
    # b stays NULL (never synced from upstream)
    TableWrite.update_event_volume(conn, c.event_id, 5000.0)

    pairs, total = TableRead.list_events_with_markets(conn, limit=10, offset=0)
    slugs = [ev.slug for ev, _ in pairs]

    assert total == 3
    assert slugs == ["c", "a", "b"]  # 5000, 100, then NULL last
    assert pairs[0][0].volume_24hr == 5000.0
    conn.close()


# ----- update_event_volume (the refresh seam) ---------------------------------


def test_update_event_volume_sets_value():
    conn = fresh_test_conn()
    e = TableWrite.upsert_event(conn, slug="x", title="X")
    TableWrite.update_event_volume(conn, e.event_id, 42.0)
    assert TableRead.get_event_by_slug(conn, "x").volume_24hr == 42.0
    conn.close()


def test_update_event_volume_noop_on_none_preserves_value():
    conn = fresh_test_conn()
    e = TableWrite.upsert_event(conn, slug="x", title="X")
    TableWrite.update_event_volume(conn, e.event_id, 42.0)
    TableWrite.update_event_volume(conn, e.event_id, None)  # must not clobber
    assert TableRead.get_event_by_slug(conn, "x").volume_24hr == 42.0
    conn.close()


# ----- capture: trending path -------------------------------------------------


def test_extract_event_metadata_pulls_volume24hr():
    meta = _extract_event_metadata(
        {"events": [{"id": "e", "slug": "s", "title": "T", "volume24hr": 7967.7}]}
    )
    assert meta is not None
    assert meta.volume_24hr == 7967.7


def test_extract_event_metadata_volume_zero_when_absent():
    meta = _extract_event_metadata({"events": [{"id": "e", "slug": "s", "title": "T"}]})
    assert meta is not None
    assert meta.volume_24hr == 0.0


# ----- serialization ----------------------------------------------------------


def test_to_gamma_event_emits_volume24hr():
    g = to_gamma_event(Event(event_id=1, slug="s", title="T", volume_24hr=12345.6), [])
    assert g.volume24hr == "12345.6"


def test_to_gamma_event_volume_zero_when_none():
    g = to_gamma_event(Event(event_id=1, slug="s", title="T"), [])
    assert g.volume24hr == "0"


# ----- all-time volume (what the cards show) ----------------------------------


def test_update_event_volume_stores_all_time_alongside_24h():
    conn = fresh_test_conn()
    e = TableWrite.upsert_event(conn, slug="x", title="X")
    TableWrite.update_event_volume(conn, e.event_id, 42.0, 253_887_562.7)
    got = TableRead.get_event_by_slug(conn, "x")
    assert got.volume_24hr == 42.0
    assert got.volume == 253_887_562.7
    conn.close()


def test_each_volume_is_skipped_independently_when_none():
    # A degraded pass carrying only one figure must not blank the other.
    conn = fresh_test_conn()
    e = TableWrite.upsert_event(conn, slug="x", title="X")
    TableWrite.update_event_volume(conn, e.event_id, 42.0, 900.0)
    TableWrite.update_event_volume(conn, e.event_id, None, 1000.0)
    got = TableRead.get_event_by_slug(conn, "x")
    assert got.volume_24hr == 42.0      # preserved
    assert got.volume == 1000.0         # updated
    TableWrite.update_event_volume(conn, e.event_id, 50.0, None)
    got = TableRead.get_event_by_slug(conn, "x")
    assert got.volume_24hr == 50.0      # updated
    assert got.volume == 1000.0         # preserved
    conn.close()


def test_extract_event_metadata_pulls_all_time_volume():
    meta = _extract_event_metadata(
        {"events": [{"id": "e", "slug": "s", "title": "T",
                     "volume24hr": 7967.7, "volume": 253_887_562.7}]}
    )
    assert meta.volume_24hr == 7967.7
    assert meta.volume == 253_887_562.7


def test_extract_event_metadata_all_time_volume_absent_is_none():
    meta = _extract_event_metadata(
        {"events": [{"id": "e", "slug": "s", "title": "T", "volume24hr": 1.0}]}
    )
    assert meta.volume is None
