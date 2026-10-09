"""GET /tags — curated order, present-only entries, nested facets, TTL cache."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agentpit.api.main import app
from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.market_state import MarketState
from agentpit.db.table_write import TableWrite
from tests.db_helpers import fresh_test_conn


@pytest.fixture()
def client() -> TestClient:
    with TestClient(app) as c:
        yield c


def _hex32(seed: str) -> str:
    return "0x" + seed.encode().hex().ljust(64, "0")[:64]


def _seed(events: dict[str, list[str]]) -> None:
    """`{event_slug: [tag_slug, …]}` — one market per event carrying the tags."""
    conn = fresh_test_conn()
    try:
        for slug, tags in events.items():
            event = TableWrite.upsert_event(conn, slug=slug, title=slug.title())
            market = TableWrite.create_market(
                conn,
                CreateMarketRequest(
                    question=f"{slug}?",
                    description="d",
                    erc1155_tokens=[(f"{slug}-y", "Yes"), (f"{slug}-n", "No")],
                    slug=slug,
                    # Derived from the slug, NOT an enumerate index: several
                    # tests call _seed twice, and a restarting counter would
                    # collide on the CONDITION_ID unique index.
                    condition_id=ConditionId(_hex32(slug)),
                    state=MarketState.ACTIVE,
                    event_id=event.event_id,
                ),
                is_polygon_market=False,
            )
            TableWrite.replace_market_tags(
                conn,
                market_id=market.market_id,
                tags=[(t, t.replace("-", " ").title()) for t in tags],
            )
    finally:
        conn.close()


def test_tags_is_empty_when_nothing_is_tagged(client):
    r = client.get("/tags")
    assert r.status_code == 200
    assert r.json() == {"tags": []}


def test_tags_hides_a_slug_below_the_threshold(client):
    """MIN_NAV_EVENTS is 10 — one politics event must not raise a tab that
    would lead to a nearly empty grid."""
    _seed({"e0": ["politics"]})
    assert client.get("/tags").json() == {"tags": []}


def test_tags_returns_present_slugs_in_curated_order(client):
    # 10 crypto events and 10 politics events; politics leads NAV_SLUGS.
    _seed(
        {f"s{i}": ["crypto"] for i in range(10)}
        | {f"p{i}": ["politics"] for i in range(10)}
    )
    tags = client.get("/tags").json()["tags"]
    assert [t["slug"] for t in tags] == ["politics", "crypto"]
    assert tags[0]["label"] == "Politics"
    assert tags[0]["count"] == 10


def test_tags_never_returns_a_slug_absent_from_the_database(client):
    _seed({f"p{i}": ["politics"] for i in range(10)})
    slugs = [t["slug"] for t in client.get("/tags").json()["tags"]]
    assert slugs == ["politics"]


def test_tags_nests_facets_ordered_by_count(client):
    # "pf" is politics-only filler: without it elections would sit at 10 of 11
    # politics events (0.909), tripping MAX_FACET_COVERAGE (0.9) and vanishing
    # from the list — the same edge case Task 4's own DAL test documents for
    # `games` at 10/11 under `sports`. Diluting the parent to 12 events keeps
    # elections' count at 10 while dropping its coverage to 0.833.
    _seed(
        {f"p{i}": ["politics", "elections"] for i in range(10)}
        | {"px": ["politics", "iran"], "pf": ["politics"]}
    )
    politics = client.get("/tags").json()["tags"][0]
    assert [(f["slug"], f["count"]) for f in politics["facets"]] == [
        ("elections", 10),
        ("iran", 1),
    ]


def test_tags_omits_blocked_slugs_from_facets(client):
    # "pf0"/"pf1" are politics-only filler: without them iran sits on all 10
    # of 10 politics events (coverage 1.0), tripping MAX_FACET_COVERAGE (0.9)
    # for the same reason it prunes `games` from `sports` in Task 4's DAL
    # tests — it would vanish for being 100% coincident with its parent, which
    # is not what this test means to exercise. Diluting the parent to 12
    # events keeps iran's count at 10 while dropping its coverage to 0.833, so
    # only `recurring`'s blocklist membership decides the outcome.
    _seed(
        {f"p{i}": ["politics", "recurring", "iran"] for i in range(10)}
        | {"pf0": ["politics"], "pf1": ["politics"]}
    )
    politics = client.get("/tags").json()["tags"][0]
    assert [f["slug"] for f in politics["facets"]] == ["iran"]


def test_tags_response_is_cached_within_the_ttl(client):
    _seed({f"p{i}": ["politics"] for i in range(10)})
    first = client.get("/tags").json()
    _seed({f"s{i}": ["crypto"] for i in range(10)})
    # Crypto now clears the threshold, but the cached response predates it.
    assert client.get("/tags").json() == first

    from agentpit.api.routes import tags as tags_route

    tags_route._tags_cache = None
    after = client.get("/tags").json()
    assert [t["slug"] for t in after["tags"]] == ["politics", "crypto"]


# ----- GET /events tag filtering ----------------------------------------------


def _event_slugs(response) -> set[str]:
    return {e["slug"] for e in response.json()}


def test_events_filters_by_tag(client):
    _seed({"a": ["politics", "trump"], "b": ["crypto", "btc"]})
    assert _event_slugs(client.get("/events?limit=10&tag=politics")) == {"a"}


def test_events_filters_by_repeated_subtag_ored(client):
    _seed(
        {
            "a": ["politics", "trump"],
            "b": ["politics", "midterms"],
            "c": ["politics", "iran"],
        }
    )
    got = _event_slugs(
        client.get("/events?limit=10&tag=politics&subtag=trump&subtag=midterms")
    )
    assert got == {"a", "b"}


def test_events_tag_is_case_insensitive(client):
    _seed({"a": ["politics"]})
    assert _event_slugs(client.get("/events?limit=10&tag=Politics")) == {"a"}


def test_events_blank_tag_does_not_collapse_the_page(client):
    _seed({"a": ["politics"], "b": ["crypto"]})
    assert len(_event_slugs(client.get("/events?limit=10&tag=%20"))) == 2


def test_events_cache_does_not_serve_a_filtered_page_to_an_unfiltered_request(client):
    """The cache key must include the tag. Without it, the filtered page below
    would be served to the unfiltered request for up to one TTL."""
    _seed({"a": ["politics"], "b": ["crypto"]})
    assert _event_slugs(client.get("/events?limit=10&offset=0&tag=politics")) == {"a"}
    assert len(_event_slugs(client.get("/events?limit=10&offset=0"))) == 2


def test_events_cache_distinguishes_subtag_sets(client):
    _seed({"a": ["politics", "trump"], "b": ["politics", "midterms"]})
    one = _event_slugs(client.get("/events?limit=10&offset=0&tag=politics&subtag=trump"))
    two = _event_slugs(
        client.get("/events?limit=10&offset=0&tag=politics&subtag=midterms")
    )
    assert one == {"a"}
    assert two == {"b"}
