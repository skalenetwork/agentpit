"""Categories the product does not carry are invisible to every browse query.

The admission policy (`CoveragePolicy.admits`) stops NEW markets arriving; these
tests cover the other half — the ~1097 Sports events already on disk when the
decision was made, which only a read-side predicate can hide.

Four queries have to agree, or the product contradicts itself: the event grid,
the market list, the "N live" headline above the grid, and the set the
liquidity mirror quotes.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.market_state import MarketState
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from tests.db_helpers import fresh_test_conn

SPORTS = ["Sports"]


@pytest.fixture()
def db() -> Any:
    conn = fresh_test_conn()
    yield conn
    conn.close()


def _hex32(seed: str) -> str:
    return "0x" + seed.encode().hex().ljust(64, "0")[:64]


def _event(db, slug: str, category: str | None):
    return TableWrite.upsert_event(
        db, slug=slug, title=slug, category=category
    )


def _market(db, name: str, event_id: int | None, *, synced: bool = True):
    cond = _hex32(name)
    request = CreateMarketRequest(
        question=f"Will {name} win?",
        description=name,
        erc1155_tokens=[(f"{cond}-yes", "Yes"), (f"{cond}-no", "No")],
        slug=name,
        condition_id=ConditionId(cond),
        state=MarketState.ACTIVE,
        event_id=event_id,
        polymarket_condition_id=_hex32(f"pm-{name}") if synced else None,
    )
    return TableWrite.create_market(db, request, is_polygon_market=False)


@pytest.fixture()
def catalogue(db):
    """One Sports event and one Politics event, a market apiece."""
    sport = _event(db, "cs2-match", "Sports")
    politics = _event(db, "who-wins-the-election", "Politics")
    _market(db, "themongolz", sport.event_id)
    _market(db, "candidate-a", politics.event_id)
    return db


# ----- the event grid --------------------------------------------------------


def test_the_event_grid_drops_the_excluded_category(catalogue):
    pairs, total = TableRead.list_events_with_markets(
        catalogue, limit=10, offset=0, excluded_categories=SPORTS
    )
    assert [ev.slug for ev, _m in pairs] == ["who-wins-the-election"]
    assert total == 1, "the page total must count what the page shows"


def test_without_the_argument_nothing_changes(catalogue):
    """Every existing caller and test passes no list; they must be unaffected."""
    pairs, total = TableRead.list_events_with_markets(catalogue, limit=10, offset=0)
    assert total == 2
    assert len(pairs) == 2


def test_an_uncategorised_event_survives_the_filter(db):
    """`LOWER(NULL) <> ALL (...)` is NULL, which WHERE reads as false — without
    the explicit IS NULL arm every uncategorised event would vanish too."""
    _event(db, "orphan-singleton", None)
    _market(db, "orphan", 1)
    pairs, total = TableRead.list_events_with_markets(
        db, limit=10, offset=0, excluded_categories=SPORTS
    )
    assert total == 1
    assert [ev.slug for ev, _m in pairs] == ["orphan-singleton"]


def test_asking_for_the_excluded_category_returns_nothing(catalogue):
    """A stale UI tab must not be a back door to the rows."""
    pairs, total = TableRead.list_events_with_markets(
        catalogue, limit=10, offset=0, category="Sports", excluded_categories=SPORTS
    )
    assert (pairs, total) == ([], 0)


def test_the_match_is_case_insensitive(catalogue):
    for spelling in ("sports", "SPORTS", " Sports "):
        _pairs, total = TableRead.list_events_with_markets(
            catalogue, limit=10, offset=0, excluded_categories=[spelling]
        )
        assert total == 1, spelling


# ----- the market list, the headline, and the mirror -------------------------


def test_the_market_list_drops_the_excluded_category(catalogue):
    markets = TableRead.list_markets_filtered(
        catalogue, limit=10, excluded_categories=SPORTS
    )
    assert [m.slug for m in markets] == ["candidate-a"]


def test_a_direct_lookup_still_resolves_an_excluded_market(catalogue):
    """Browsing hides it; addressing it by slug does not 404. The MCP desk
    resolves one known slug through here and must keep working."""
    markets = TableRead.list_markets_filtered(catalogue, slug="themongolz", limit=1)
    assert [m.slug for m in markets] == ["themongolz"]


def _sided(*names: str) -> list[str]:
    return [f"{_hex32(name)}-yes" for name in names]


def test_the_live_headline_counts_what_the_grid_shows(catalogue):
    sided = _sided("themongolz", "candidate-a")
    assert TableRead.count_open_markets(catalogue, sided) == 2
    assert (
        TableRead.count_open_markets(catalogue, sided, excluded_categories=SPORTS) == 1
    )
    assert TableRead.count_open_markets(catalogue, _sided("candidate-a")) == 1


def test_the_mirror_stops_quoting_the_excluded_category(catalogue):
    """Quoting a market the catalogue refuses to list is pure gas."""
    before = TableRead.list_active_synced_markets(catalogue)
    assert {m.slug for m in before} == {"themongolz", "candidate-a"}
    after = TableRead.list_active_synced_markets(
        catalogue, excluded_categories=SPORTS
    )
    assert [m.slug for m in after] == ["candidate-a"]


def test_a_market_with_no_event_is_kept(db):
    """Exclude only on positive evidence: no event means no category, which is
    not the same as being in an excluded one."""
    _market(db, "unbound", None)
    markets = TableRead.list_markets_filtered(
        db, limit=10, excluded_categories=SPORTS
    )
    assert [m.slug for m in markets] == ["unbound"]
    assert (
        TableRead.count_open_markets(db, _sided("unbound"), excluded_categories=SPORTS)
        == 1
    )


def test_an_empty_list_excludes_nothing(catalogue):
    """How the whole feature is switched off without a code change."""
    for empty in ([], None, [""], ["   "]):
        assert (
            TableRead.count_open_markets(
                catalogue,
                _sided("themongolz", "candidate-a"),
                excluded_categories=empty,
            )
            == 2
        ), empty


# ----- the sidebar ------------------------------------------------------------


def _tagged(db, market_id: int, slug: str):
    TableWrite.replace_market_tags(
        db, market_id=market_id, tags=[(slug, slug.title())]
    )


def test_an_excluded_category_gets_no_sidebar_entry(db):
    """The sidebar is built from the TAG graph, not the CATEGORY column, so
    filtering the category list alone left a "Sports 2035" entry whose every
    click returned an empty grid. Counting excluded events out drops the slug
    below `min_events` on its own."""
    sport = _event(db, "cs2-match", "Sports")
    politics = _event(db, "election", "Politics")
    m1 = _market(db, "themongolz", sport.event_id)
    m2 = _market(db, "candidate-a", politics.event_id)
    _tagged(db, m1.market_id, "sports")
    _tagged(db, m2.market_id, "politics")

    unfiltered = TableRead.list_tag_nav(db, slugs=["sports", "politics"], min_events=1)
    assert {s for s, _l, _c in unfiltered} == {"sports", "politics"}

    filtered = TableRead.list_tag_nav(
        db, slugs=["sports", "politics"], min_events=1, excluded_categories=SPORTS
    )
    assert [s for s, _l, _c in filtered] == ["politics"]


def test_a_tag_that_survives_on_other_events_keeps_its_place(db):
    """`esports` events that upstream never filed under Sports are still real
    listings — the count drops, the entry stays."""
    sport = _event(db, "cs2-match", "Sports")
    other = _event(db, "gamedev-award", "Pop Culture")
    m1 = _market(db, "themongolz", sport.event_id)
    m2 = _market(db, "best-indie", other.event_id)
    _tagged(db, m1.market_id, "esports")
    _tagged(db, m2.market_id, "esports")

    rows = TableRead.list_tag_nav(
        db, slugs=["esports"], min_events=1, excluded_categories=SPORTS
    )
    assert rows == [("esports", "Esports", 1)]


# ----- the tag half of the exclusion ------------------------------------------

ESPORTS = ["esports"]


def test_a_tagged_event_is_excluded_though_its_category_is_not(db):
    """Upstream files "LPL 2026 Season Winner" and "Will Valve release Deadlock
    before 2027?" under Technology/Culture while tagging them `esports`, so a
    category-only rule left three of them listed."""
    tech = _event(db, "deadlock-2027", "Technology")
    m = _market(db, "deadlock", tech.event_id)
    _tagged(db, m.market_id, "esports")

    pairs, total = TableRead.list_events_with_markets(
        db, limit=10, offset=0, excluded_categories=SPORTS, excluded_tags=ESPORTS
    )
    assert (pairs, total) == ([], 0)
    assert (
        TableRead.list_markets_filtered(
            db, limit=10, excluded_categories=SPORTS, excluded_tags=ESPORTS
        )
        == []
    )
    assert (
        TableRead.count_open_markets(
            db, _sided("deadlock"), excluded_categories=SPORTS, excluded_tags=ESPORTS
        )
        == 0
    )


def test_the_tag_exclusion_does_not_reach_untagged_events(db):
    politics = _event(db, "election", "Politics")
    _market(db, "candidate-a", politics.event_id)
    _pairs, total = TableRead.list_events_with_markets(
        db, limit=10, offset=0, excluded_categories=SPORTS, excluded_tags=ESPORTS
    )
    assert total == 1


def test_one_tagged_market_excludes_its_whole_event(db):
    """The tag lands on markets, the listing is of events: a single tagged leg
    is enough, or a multi-outcome event would half-vanish."""
    ev = _event(db, "mixed", "Technology")
    m1 = _market(db, "leg-a", ev.event_id)
    _market(db, "leg-b", ev.event_id)
    _tagged(db, m1.market_id, "esports")
    _pairs, total = TableRead.list_events_with_markets(
        db, limit=10, offset=0, excluded_tags=ESPORTS
    )
    assert total == 0
