"""Pure-function tests for capturing upstream Polymarket event metadata.

These do NOT touch anvil/on-chain — they exercise the parsers and the
DB-only binding helper.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.market_state import MarketState
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.polymarket.polymarket_sync import (
    UpstreamMarket,
    _extract_event_metadata,
    _extract_outcome_metadata,
    bind_market_to_upstream_event,
    parse,
)
from tests.chain_fakes import gamma_row
from tests.db_helpers import fresh_test_conn


@pytest.fixture()
def db() -> Any:
    conn = fresh_test_conn()
    yield conn
    conn.close()


def _hex32(seed: str) -> str:
    return "0x" + seed.encode().hex().ljust(64, "0")[:64]


def _upstream(**over) -> UpstreamMarket:
    m = parse(gamma_row(**over))
    assert isinstance(m, UpstreamMarket), m
    return m


# ----- _extract_event_metadata ------------------------------------------------


def test_extract_event_metadata_returns_none_when_events_missing():
    assert _extract_event_metadata({}) is None
    assert _extract_event_metadata({"events": []}) is None


def test_extract_event_metadata_pulls_first_event():
    pm = {
        # Category comes from the market's own tags — the nested event object
        # carries no tags, and its `category` field is null upstream.
        "tags": [{"id": "1", "label": "Sports", "slug": "sports"}],
        "events": [
            {
                "id": "evt-1",
                "slug": "2026-fifa-world-cup-winner",
                "title": "2026 FIFA World Cup Winner",
                "description": "Who lifts the cup?",
                "image": "https://img/wc.png",
                "startDate": "2026-06-11T00:00:00Z",
                "endDate": "2026-07-19T22:00:00Z",
            }
        ],
    }
    meta = _extract_event_metadata(pm)
    assert meta is not None
    assert meta.slug == "2026-fifa-world-cup-winner"
    assert meta.title == "2026 FIFA World Cup Winner"
    assert meta.polymarket_event_id == "evt-1"
    assert meta.icon_url == "https://img/wc.png"
    assert _upstream(**pm).category == "Sports"
    # Dates are converted to unix int when parseable.
    assert isinstance(meta.start_date, int)
    assert isinstance(meta.end_date, int)


def test_extract_event_metadata_handles_missing_optional_fields():
    pm = {"events": [{"id": "x", "slug": "x", "title": "X"}]}
    meta = _extract_event_metadata(pm)
    assert meta is not None
    assert meta.icon_url is None
    assert meta.start_date is None
    assert meta.end_date is None


def test_extract_event_metadata_ignores_the_dead_nested_category_field():
    """Gamma returns `category: null` on every nested event.

    If it ever starts returning a value, the tag-derived category still wins —
    tags are the taxonomy Polymarket actually maintains.
    """
    pm = {
        "tags": [{"slug": "crypto"}],
        "events": [{"id": "x", "slug": "x", "title": "X", "category": "Sports"}],
    }
    assert _upstream(**pm).category == "Crypto"


def test_extract_event_metadata_survives_malformed_tags():
    pm = {
        "tags": [
            None,
            "not-a-dict",
            {"slug": None},
            {},
            {"slug": 123},
            {"slug": ["a"]},
            {"slug": "sports"},
        ],
        "events": [{"id": "x", "slug": "x", "title": "X"}],
    }
    assert _upstream(**pm).category == "Sports"


def test_extract_event_metadata_category_is_none_without_recognisable_tags():
    pm = {
        "tags": [{"slug": "gta-vi"}, {"slug": "all"}],
        "events": [{"id": "x", "slug": "x", "title": "X"}],
    }
    assert _upstream(**pm).category is None


# ----- _extract_outcome_metadata ----------------------------------------------


def test_extract_outcome_metadata_returns_group_item_title_and_image():
    pm = {"groupItemTitle": "France", "image": "https://flags/fr.svg"}
    label, icon = _extract_outcome_metadata(pm)
    assert label == "France"
    assert icon == "https://flags/fr.svg"


def test_extract_outcome_metadata_handles_missing_keys():
    label, icon = _extract_outcome_metadata({})
    assert label is None
    assert icon is None


def test_parse_captures_outcome_metadata():
    m = _upstream(groupItemTitle="France", image="https://flags/fr.svg")
    assert m.label == "France"
    assert m.icon == "https://flags/fr.svg"


# ----- bind_market_to_upstream_event (DB-only) --------------------------------


def _seed_market(db, *, question: str, cond_id: str):
    req = CreateMarketRequest(
        question=question,
        description=f"d-{question}",
        erc1155_tokens=[(f"{cond_id}-y", "Yes"), (f"{cond_id}-n", "No")],
        slug=question.lower().replace(" ", "-").replace("?", ""),
        condition_id=ConditionId(cond_id),
        state=MarketState.ACTIVE,
    )
    return TableWrite.create_market(db, req, is_polygon_market=False)


def test_bind_market_to_upstream_event_creates_event_and_attaches(db):
    market = _seed_market(db, question="will france win?", cond_id=_hex32("fr"))
    pm = {
        "events": [
            {
                "id": "evt-1",
                "slug": "wc",
                "title": "2026 FIFA World Cup Winner",
                "image": "https://img/wc.png",
                "category": "Sports",
            }
        ],
        "groupItemTitle": "France",
        "image": "https://flags/fr.svg",
    }

    bind_market_to_upstream_event(db, market.market_id, _upstream(**pm))

    re = TableRead.read_market(db, market.market_id)
    assert re is not None and re.event_id is not None
    assert re.outcome_label == "France"
    assert re.icon_url == "https://flags/fr.svg"

    event = TableRead.get_event_by_slug(db, "wc")
    assert event is not None
    assert event.title == "2026 FIFA World Cup Winner"
    assert event.icon_url == "https://img/wc.png"
    assert event.polymarket_event_id == "evt-1"


def test_bind_market_to_upstream_event_reuses_existing_event_by_polymarket_id(db):
    """Two markets with the same upstream event id share one local event row."""
    m1 = _seed_market(db, question="france?", cond_id=_hex32("fr"))
    m2 = _seed_market(db, question="spain?", cond_id=_hex32("es"))
    pm_template = {
        "events": [{"id": "evt-1", "slug": "wc", "title": "WC"}],
    }
    bind_market_to_upstream_event(db, m1.market_id, _upstream(**{**pm_template, "groupItemTitle": "France"}))
    bind_market_to_upstream_event(db, m2.market_id, _upstream(**{**pm_template, "groupItemTitle": "Spain"}))

    rm1 = TableRead.read_market(db, m1.market_id)
    rm2 = TableRead.read_market(db, m2.market_id)
    assert rm1 is not None and rm2 is not None
    assert rm1.event_id == rm2.event_id


def test_bind_market_to_upstream_event_noop_when_no_event_metadata(db):
    market = _seed_market(db, question="solo?", cond_id=_hex32("solo"))
    bind_market_to_upstream_event(db, market.market_id, _upstream(events=[]))
    re = TableRead.read_market(db, market.market_id)
    assert re is not None and re.event_id is None


def test_bind_market_to_upstream_event_rebinds_from_singleton_to_real_event(db):
    """A market auto-wrapped in a singleton event must rebind when the real
    upstream event arrives on a later sync. Without this, pre-existing
    markets stay in their singleton events forever after the feature lands.
    """
    market = _seed_market(db, question="france?", cond_id=_hex32("fr"))

    # Simulate the auto-wrap that runs at startup: market gets a singleton.
    singleton = TableWrite.upsert_event(db, slug=market.slug, title=market.question)
    TableWrite.attach_market_to_event(
        db, market_id=market.market_id, event_id=singleton.event_id
    )

    # Later sync brings the real multi-market event.
    pm = {
        "events": [{"id": "evt-wc", "slug": "wc", "title": "WC"}],
        "groupItemTitle": "France",
        "image": "https://flags/fr.svg",
    }
    bind_market_to_upstream_event(db, market.market_id, _upstream(**pm))

    re = TableRead.read_market(db, market.market_id)
    real_event = TableRead.get_event_by_slug(db, "wc")
    assert re is not None and real_event is not None
    assert re.event_id == real_event.event_id  # rebound to the real event
    assert re.outcome_label == "France"


def test_bind_market_sets_category_on_a_new_event(db):
    market = _seed_market(db, question="will france win?", cond_id=_hex32("fr"))
    pm = {
        "tags": [{"slug": "sports"}],
        "events": [{"id": "evt-1", "slug": "wc", "title": "WC"}],
    }

    bind_market_to_upstream_event(db, market.market_id, _upstream(**pm))

    event = TableRead.get_event_by_slug(db, "wc")
    assert event is not None and event.category == "Sports"


def test_bind_market_upgrades_an_existing_event_to_a_stricter_category(db):
    """Sibling markets resolve independently; the strictest one wins."""
    m1 = _seed_market(db, question="france?", cond_id=_hex32("fr"))
    m2 = _seed_market(db, question="spain?", cond_id=_hex32("es"))
    pm_events = [{"id": "evt-1", "slug": "wc", "title": "WC"}]

    bind_market_to_upstream_event(
        db, m1.market_id, _upstream(**{"events": pm_events, "tags": [{"slug": "politics"}]})
    )
    bind_market_to_upstream_event(
        db, m2.market_id, _upstream(**{"events": pm_events, "tags": [{"slug": "sports"}]})
    )

    event = TableRead.get_event_by_slug(db, "wc")
    assert event is not None and event.category == "Sports"


def test_bind_market_does_not_downgrade_an_existing_event_category(db):
    """Same pair as above in the opposite order — the result must not change."""
    m1 = _seed_market(db, question="france?", cond_id=_hex32("fr"))
    m2 = _seed_market(db, question="spain?", cond_id=_hex32("es"))
    pm_events = [{"id": "evt-1", "slug": "wc", "title": "WC"}]

    bind_market_to_upstream_event(
        db, m1.market_id, _upstream(**{"events": pm_events, "tags": [{"slug": "sports"}]})
    )
    bind_market_to_upstream_event(
        db, m2.market_id, _upstream(**{"events": pm_events, "tags": [{"slug": "politics"}]})
    )

    event = TableRead.get_event_by_slug(db, "wc")
    assert event is not None and event.category == "Sports"


def test_bind_market_never_clears_a_stored_category(db):
    """An unresolvable market must not undo a sibling's categorization."""
    m1 = _seed_market(db, question="france?", cond_id=_hex32("fr"))
    m2 = _seed_market(db, question="spain?", cond_id=_hex32("es"))
    pm_events = [{"id": "evt-1", "slug": "wc", "title": "WC"}]

    bind_market_to_upstream_event(
        db, m1.market_id, _upstream(**{"events": pm_events, "tags": [{"slug": "sports"}]})
    )
    bind_market_to_upstream_event(
        db, m2.market_id, _upstream(**{"events": pm_events, "tags": [{"slug": "gta-vi"}]})
    )

    event = TableRead.get_event_by_slug(db, "wc")
    assert event is not None and event.category == "Sports"


def test_bind_market_converges_on_the_strictest_category_regardless_of_order(db):
    """Three markets, three categories, arriving loosest-first.

    Pins that _sync_event_category compares ranks rather than writing
    unconditionally: an unconditional write would leave the last arrival
    ("World"), and the pre-fix code would leave the first ("Politics").
    """
    m1 = _seed_market(db, question="france?", cond_id=_hex32("fr"))
    m2 = _seed_market(db, question="spain?", cond_id=_hex32("es"))
    m3 = _seed_market(db, question="brazil?", cond_id=_hex32("br"))
    pm_events = [{"id": "evt-1", "slug": "wc", "title": "WC"}]

    bind_market_to_upstream_event(
        db, m1.market_id, _upstream(**{"events": pm_events, "tags": [{"slug": "politics"}]})
    )
    bind_market_to_upstream_event(
        db, m2.market_id, _upstream(**{"events": pm_events, "tags": [{"slug": "sports"}]})
    )
    bind_market_to_upstream_event(
        db, m3.market_id, _upstream(**{"events": pm_events, "tags": [{"slug": "world"}]})
    )

    event = TableRead.get_event_by_slug(db, "wc")
    assert event is not None and event.category == "Sports"


def test_bind_market_categorizes_a_previously_uncategorized_event(db):
    """An event bound before its markets carried tags acquires its category
    when a tagged market binds to it, with no migration script."""
    market = _seed_market(db, question="france?", cond_id=_hex32("fr"))
    pm_events = [{"id": "evt-1", "slug": "wc", "title": "WC"}]
    bind_market_to_upstream_event(db, market.market_id, _upstream(events=pm_events))
    seeded = TableRead.get_event_by_slug(db, "wc")
    assert seeded is not None and seeded.category is None

    bind_market_to_upstream_event(
        db,
        market.market_id,
        _upstream(events=pm_events, tags=[{"slug": "sports"}]),
    )

    event = TableRead.get_event_by_slug(db, "wc")
    assert event is not None and event.category == "Sports"


def test_bind_market_stores_the_game_fields_and_tags(db):
    market = _seed_market(db, question="game?", cond_id=_hex32("game"))
    bind_market_to_upstream_event(
        db,
        market.market_id,
        _upstream(
            gameStartTime="2026-10-05 14:00:00+00",
            events=[{"id": "g", "slug": "nfl-game", "title": "Game", "gameId": 19517, "seriesSlug": "nfl-2026"}],
            tags=[{"slug": "nfl", "label": "NFL"}],
        ),
    )
    event = TableRead.get_event_by_slug(db, "nfl-game")
    assert event is not None
    assert (event.start_time, event.game_id, event.series_slug) == (
        1791208800, "19517", "nfl-2026"
    )
    assert TableRead.read_market(db, market.market_id).event_id == event.event_id
