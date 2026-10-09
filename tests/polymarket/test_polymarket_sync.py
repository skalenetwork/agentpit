import dataclasses
import time

import httpx
import pytest

from agentpit.config import Settings
from agentpit.polymarket import polymarket_sync
from agentpit.polymarket.polymarket_sync import (
    CoveragePolicy,
    UpstreamMarket,
    Verdict,
    parse,
    resolutions,
    walk_admissions,
)
from agentpit.utils.parse import _iso_to_unix
from tests.chain_fakes import gamma_row

POLICY = CoveragePolicy(
    10_000.0,
    True,
    frozenset({"sports"}),
    frozenset({"sports", "esports"}),
    (),
    10_000.0,
)
GAMES = CoveragePolicy(
    10_000.0, True, frozenset(), frozenset(), (100351, 450), 10_000.0
)


def _market(**over) -> UpstreamMarket:
    m = parse(gamma_row(**over))
    assert isinstance(m, UpstreamMarket), m
    return m




def test_a_gamma_row_parses_into_a_typed_binary_market():
    row = gamma_row(
        clobTokenIds='["111", "222"]',
        outcomes='["Up", "Down"]',
        groupItemTitle="Up",
        image="https://img/up.png",
        oneDayPriceChange=0.04,
        liquidityNum=12_500.5,
        tags=[{"slug": "crypto", "label": "Crypto"}],
    )
    m = parse(row)
    assert isinstance(m, UpstreamMarket)
    assert (m.liquidity, _market().liquidity) == (12_500.5, 0.0)
    assert (m.pm_id, m.condition, m.tokens, m.labels) == (
        int(row["id"]),
        row["conditionId"],
        ("111", "222"),
        ("Up", "Down"),
    )
    assert (m.label, m.icon, m.price_change_24h, m.tags, m.category) == (
        "Up",
        "https://img/up.png",
        0.04,
        (("crypto", "Crypto"),),
        "Crypto",
    )
    assert (m.start_date, m.end_date, m.closed, m.accepting) == (
        1767225600,
        4070908800,
        False,
        True,
    )
    assert m.event is not None and m.event.slug == row["events"][0]["slug"]


def test_only_binary_v1_rows_parse():
    assert (
        parse(gamma_row(outcomes='["A", "B", "C"]', clobTokenIds='["1", "2", "3"]'))
        is Verdict.NOT_BINARY
    )
    assert parse(gamma_row(version="v2")) is Verdict.NOT_V1
    assert parse(gamma_row(version=None)) is Verdict.NOT_V1


@pytest.mark.parametrize(
    "over",
    [
        {"conditionId": "0x12"},
        {"conditionId": None},
        {"id": "x"},
        {"question": " "},
        {"description": ""},
        {"slug": None},
        {"startDate": "soon"},
        {"endDate": "2025-01-01T00:00:00Z"},
        {"clobTokenIds": '["a", "b"]'},
        {"tags": None},
        {"tags": "politics"},
    ],
)
def test_a_malformed_row_is_a_verdict_never_a_raise(over):
    assert parse(gamma_row(**over)) is Verdict.MALFORMED


def test_anything_but_a_dict_is_malformed():
    for row in (None, [], "row", 3):
        assert parse(row) is Verdict.MALFORMED


def test_the_event_start_time_wins_over_the_market_game_start_time():
    meta = polymarket_sync._extract_event_metadata(
        {
            "gameStartTime": "2026-10-05 13:00:00+00",
            "gameId": "1711434",
            "events": [
                {
                    "id": "1",
                    "slug": "cs2",
                    "title": "CS2",
                    "startTime": "2026-10-05T14:00:00Z",
                    "gameId": 1711434,
                    "seriesSlug": "counter-strike",
                }
            ],
        }
    )
    assert meta is not None
    assert (meta.start_time, meta.game_id, meta.series_slug) == (
        1791208800,
        "1711434",
        "counter-strike",
    )


def test_the_start_time_falls_back_to_the_market_game_start_time():
    meta = polymarket_sync._extract_event_metadata(
        {
            "gameStartTime": "2026-10-05 14:00:00+00",
            "gameId": "1711434",
            "events": [{"id": "1", "slug": "cs2", "title": "CS2"}],
        }
    )
    assert meta is not None
    assert (meta.start_time, meta.game_id, meta.series_slug) == (
        1791208800,
        "1711434",
        None,
    )


# ----- the series that regenerate faster than anyone reads them --------------


def _kept(
    *, exclude_churn_series=True, excluded_categories=(), excluded_tags=(), **over
):
    """Does this market survive the catalogue filter?"""
    policy = CoveragePolicy.from_settings(
        Settings(
            _env_file=None,
            sync_min_volume_24h=0,
            sync_exclude_churn_series=exclude_churn_series,
            excluded_categories=list(excluded_categories),
            excluded_tags=list(excluded_tags),
        )
    )
    return policy.admits(_market(**over)) is Verdict.CARRY


def test_the_fee_type_alone_never_drops_a_weather_market():
    """`weather_fees` is upstream's schedule for the WHOLE weather / science /
    natural-disaster bucket, not a name for the daily-temperature series. Of
    158 rows carrying it in a live 2000-row sample, 5 are long-lived markets
    nothing like the churn series — dropping on the fee type alone thinned that
    category silently, with no error and no log line."""
    survivors = (
        [],
        # $412k book, $17.7M lifetime volume, endDate 2026-12-31.
        ["pandemics", "weather", "hantavirus"],
        ["science", "weather", "climate-science", "global-temp"],
        ["science", "weather", "global-temp"],
        ["science", "weather", "natural-disasters", "climate-science"],
        ["f1", "dutch", "weather", "climate", "formula1", "grand-prix"],
    )
    for slugs in survivors:
        assert (
            _kept(
                feeType="weather_fees",
                tags=[{"slug": s, "label": s} for s in slugs],
            )
            is True
        ), slugs


def test_a_tagged_daily_temperature_market_is_still_dropped():
    """The 155 real churn rows all carry the tag alongside the fee type."""
    assert (
        _kept(
            feeType="weather_fees",
            tags=[
                {"slug": "weather", "label": "Weather"},
                {"slug": "recurring", "label": "Recurring"},
                {"slug": "hide-from-new", "label": "Hide From New"},
                {"slug": "daily-temperature", "label": "Daily Temperature"},
                {"slug": "munich", "label": "Munich"},
            ],
        )
        is False
    )


def test_the_daily_temperature_tag_alone_is_enough():
    """The tag is an independent signal, so a weather market whose feeType
    upstream ever renames is still recognised."""
    assert (
        _kept(
            tags=[
                {"slug": "weather", "label": "Weather"},
                {"slug": "daily-temperature", "label": "Daily Temperature"},
                {"slug": "munich", "label": "Munich"},
            ]
        )
        is False
    )


def test_a_sports_prop_is_dropped():
    """Spreads and team totals hang off a game we already carry."""
    for prop in ("spreads", "team_totals", "soccer_exact_score"):
        assert _kept(feeType="sports_fees_v2", sportsMarketType=prop) is False, prop


def test_the_game_and_its_per_game_winners_are_kept():
    for kind in ("moneyline", "child_moneyline"):
        assert _kept(feeType="sports_fees_v2", sportsMarketType=kind) is True, kind


def test_a_market_carrying_neither_field_is_kept():
    assert _kept() is True


def test_a_crypto_market_is_kept():
    """crypto_fees_v2 was 101 of a live 400-market sample and carries no
    sportsMarketType at all."""
    assert _kept(feeType="crypto_fees_v2") is True


def test_malformed_tag_entries_are_skipped_not_raised_on():
    for tags in (["daily-temperature"], [None, 3], [{"slug": None}]):
        assert _kept(tags=tags) is True, tags


def test_the_flag_switches_the_exclusion_back_off():
    """The operator can reverse the decision without a code change."""
    assert (
        _kept(
            tags=[{"slug": "daily-temperature", "label": "Daily Temperature"}],
            exclude_churn_series=False,
        )
        is True
    )




def test_a_sports_market_is_dropped_by_its_category():
    assert (
        _kept(
            excluded_categories=["Sports"],
            sportsMarketType="moneyline",
            tags=[{"slug": "sports", "label": "Sports"}],
        )
        is False
    )


def test_an_esports_market_is_dropped_though_it_carries_no_tags():
    """The shape this was built for. `cs2-mgc-mglz-2026-08-12` arrives from
    Gamma with `tags: []` and `sportsMarketType: 'moneyline'`, so
    `resolve_category` has nothing to reduce and only the upstream sports field
    identifies it. A tag-only check would let every esports match through."""
    assert (
        _kept(excluded_categories=["Sports"], sportsMarketType="moneyline", tags=[])
        is False
    )


def test_a_headline_game_is_dropped_even_though_the_churn_filter_keeps_it():
    assert _kept(sportsMarketType="moneyline") is True
    assert _kept(sportsMarketType="moneyline", excluded_categories=["Sports"]) is False


def test_the_category_match_is_case_insensitive():
    """The setting is operator-typed; `resolve_category` returns "Sports"."""
    for spelling in ("sports", "SPORTS", "  Sports  "):
        assert (
            _kept(excluded_categories=[spelling], sportsMarketType="moneyline") is False
        ), spelling


def test_a_market_outside_the_excluded_categories_is_untouched():
    assert (
        _kept(
            excluded_categories=["Sports"],
            tags=[{"slug": "politics", "label": "Politics"}],
        )
        is True
    )


def test_an_empty_exclusion_list_carries_everything():
    assert _kept(excluded_categories=[], sportsMarketType="moneyline") is True


def test_the_exclusion_is_independent_of_the_churn_flag():
    assert (
        _kept(
            exclude_churn_series=False,
            excluded_categories=["Sports"],
            sportsMarketType="spreads",
        )
        is False
    )


def test_an_excluded_tag_keeps_a_market_out_whatever_its_category():
    esports = [
        {"slug": "esports", "label": "Esports"},
        {"slug": "tech", "label": "Tech"},
    ]
    assert _kept(tags=esports) is True
    assert _kept(tags=esports, excluded_tags=[" Esports "]) is False


def test_the_floor_and_the_trading_flags_gate_admission():
    assert POLICY.admits(_market(volume24hr=10_000)) is Verdict.CARRY
    assert POLICY.admits(_market(volume24hr=9_999.99)) is Verdict.LOW_VOLUME
    assert POLICY.admits(_market(closed=True)) is Verdict.NOT_TRADING
    assert POLICY.admits(_market(acceptingOrders=False)) is Verdict.NOT_TRADING
    assert POLICY.admits(_market(acceptingOrders=None)) is Verdict.NOT_TRADING
    assert POLICY.admits(_market(active=False)) is Verdict.NOT_TRADING


def test_a_game_is_admitted_by_book_depth_whatever_its_volume():
    game = {"sportsMarketType": "moneyline", "volume24hr": 0, "liquidityNum": 10_000}

    def verdict(policy: CoveragePolicy = GAMES, **over) -> Verdict:
        return policy.admits_game(_market(**game | over))

    assert verdict() is Verdict.CARRY
    assert verdict(liquidityNum=9_999.99) is Verdict.THIN_BOOK
    for kind in ("spreads", "totals", None):
        assert verdict(sportsMarketType=kind) is Verdict.THIN_BOOK, kind
    assert verdict(closed=True) is Verdict.NOT_TRADING
    sports = [{"slug": "sports", "label": "Sports"}]
    assert verdict(POLICY, tags=sports) is Verdict.EXCLUDED


def test_the_defaults_exclude_nothing_and_walk_football_by_depth():
    assert CoveragePolicy.from_settings(Settings(_env_file=None)) == CoveragePolicy(
        1_000.0, True, frozenset(), frozenset(), (100351, 450), 10_000.0
    )


def _gamma(pages: list[list[dict]], seen: list[httpx.Request]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/events":
            return httpx.Response(200, json=[])
        seen.append(request)
        i = len(seen) - 1
        body: dict = {"markets": pages[i]}
        if i + 1 < len(pages):
            body["next_cursor"] = f"c{i + 1}"
        return httpx.Response(200, json=body)

    return httpx.Client(
        base_url="https://gamma.test", transport=httpx.MockTransport(handler)
    )


def test_the_walk_pages_by_24h_volume_until_the_floor():
    rows = [
        gamma_row(volume24hr=v) for v in (90_000, 40_000, 20_000, 12_000, 9_000, 8_000)
    ]
    seen: list[httpx.Request] = []
    admitted = walk_admissions(
        _gamma([rows[:2], rows[2:4], rows[4:], [gamma_row()]], seen), POLICY, set()
    )
    assert [m.volume_24hr for m in admitted] == [90_000, 40_000, 20_000, 12_000]
    assert len(seen) == 3
    params = seen[0].url.params
    assert (params["order"], params["ascending"], params["limit"]) == (
        "volume24hr",
        "false",
        "100",
    )
    assert (params["closed"], params["include_tag"]) == ("false", "true")
    assert "after_cursor" not in params
    assert [r.url.params.get("after_cursor") for r in seen[1:]] == ["c1", "c2"]
    assert len({r.url.params["_cb"] for r in seen}) == 3


def test_the_walk_admits_only_what_the_policy_carries():
    page = [
        gamma_row(),
        gamma_row(sportsMarketType="spreads"),
        gamma_row(version="v2"),
        gamma_row(acceptingOrders=False),
        gamma_row(tags=[{"slug": "sports", "label": "Sports"}]),
    ]
    assert [
        m.condition for m in walk_admissions(_gamma([page], []), POLICY, set())
    ] == [page[0]["conditionId"]]


def test_each_game_tag_is_walked_by_depth_after_the_volume_walk():
    game = {"sportsMarketType": "moneyline", "volume24hr": 0, "liquidityNum": 20_000}
    cfb, later = gamma_row(**game), gamma_row(**game)
    spread = gamma_row(**game | {"sportsMarketType": "spreads"})
    thin = gamma_row(**game | {"liquidityNum": 9_000})
    pages = {
        (None, None): {"markets": [gamma_row(volume24hr=5_000)], "next_cursor": "v1"},
        ("100351", None): {"markets": [cfb, spread], "next_cursor": "c1"},
        ("100351", "c1"): {"markets": [later]},
        ("450", None): {"markets": [thin]},
    }
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/events":
            return httpx.Response(200, json=[])
        seen.append(request)
        params = request.url.params
        return httpx.Response(
            200, json=pages[params.get("tag_id"), params.get("after_cursor")]
        )

    gamma = httpx.Client(
        base_url="https://gamma.test", transport=httpx.MockTransport(handler)
    )
    admitted = walk_admissions(gamma, GAMES, set())
    assert [m.condition for m in admitted] == [cfb["conditionId"], later["conditionId"]]
    assert len(seen) == 4
    params = seen[1].url.params
    assert "order" not in params
    assert (params["sports_market_types"], params["liquidity_num_min"]) == (
        "moneyline",
        "10000.0",
    )


def test_a_gamma_error_fails_the_walk():
    gamma = httpx.Client(
        base_url="https://gamma.test",
        transport=httpx.MockTransport(lambda request: httpx.Response(503)),
    )
    with pytest.raises(httpx.HTTPStatusError):
        walk_admissions(gamma, POLICY, set())


def _event_walk(
    page: list[dict], events: dict[str, list[dict]], seen: list[httpx.Request]
) -> httpx.Client:
    rows = {r["conditionId"]: r for members in events.values() for r in members}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        params = request.url.params
        if request.url.path == "/markets/keyset":
            return httpx.Response(200, json={"markets": page})
        if request.url.path == "/events":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": i,
                        "tags": [],
                        "markets": [
                            {k: v for k, v in r.items() if k not in ("tags", "events")}
                            for r in events[i]
                        ],
                    }
                    for i in params.get_list("id")
                ],
            )
        closed = params["closed"] == "true"
        return httpx.Response(
            200,
            json=[
                rows[c]
                for c in params.get_list("condition_ids")
                if (rows[c]["closed"] is True) == closed
            ],
        )

    return httpx.Client(
        base_url="https://gamma.test", transport=httpx.MockTransport(handler)
    )


def test_a_sibling_excluded_on_its_own_tags_still_uses_one_of_the_twelve_slots():
    event = {"id": "e1", "slug": "e1", "title": "E1"}
    parent = gamma_row(events=[event])
    ranked = [gamma_row(volume24hr=v, events=[event]) for v in range(13, 0, -1)]
    ranked[0]["tags"] = [{"slug": "esports", "label": "Esports"}]
    prop = gamma_row(volume24hr=40_000, sportsMarketType="spreads", events=[event])
    shut = gamma_row(volume24hr=30_000, closed=True, events=[event])
    seen: list[httpx.Request] = []
    gamma = _event_walk([parent], {"e1": [shut, prop, *ranked, parent]}, seen)

    admitted = walk_admissions(gamma, POLICY, set())

    assert [m.condition for m in admitted] == [
        r["conditionId"] for r in [parent, *ranked[1:11]]
    ]
    _, events, markets = seen
    assert events.url.params.get_list("id") == ["e1"]
    assert (events.url.params["limit"], "_cb" in events.url.params) == ("40", True)
    assert markets.url.params.get_list("condition_ids") == [
        r["conditionId"] for r in ranked[:11]
    ]


def test_events_are_asked_forty_and_siblings_fifty_at_a_time():
    events = [{"id": f"e{i}", "slug": f"e{i}", "title": f"E{i}"} for i in range(41)]
    members = {e["id"]: [gamma_row(events=[e]) for _ in range(3)] for e in events}
    seen: list[httpx.Request] = []
    gamma = _event_walk(
        [rows[0] for rows in members.values()] + [members["e0"][1]], members, seen
    )

    admitted = walk_admissions(gamma, POLICY, set())

    assert len({m.condition for m in admitted}) == 41 * 3
    assert [
        (
            r.url.path,
            len(r.url.params.get_list("id") or r.url.params.get_list("condition_ids")),
        )
        for r in seen[1:]
    ] == [("/events", 40), ("/events", 1), ("/markets", 50), ("/markets", 31)]


def test_only_a_market_not_yet_carried_outside_a_game_brings_its_event():
    carried, game, new = (
        gamma_row(events=[{"id": i, "slug": i, "title": i} | extra])
        for i, extra in (("e1", {}), ("e2", {"gameId": 7}), ("e3", {}))
    )
    seen: list[httpx.Request] = []
    gamma = _event_walk(
        [carried, game, new], {"e1": [carried], "e2": [game], "e3": [new]}, seen
    )

    walk_admissions(gamma, POLICY, {carried["conditionId"]})

    assert [r.url.params.get_list("id") for r in seen[1:]] == [["e3"]]


def test_series_windows_come_from_one_events_call_without_a_sibling_fetch():
    up = [{"slug": "up-or-down", "label": "Up or Down"}]
    live, next_, shut = (
        gamma_row(outcomes='["Up", "Down"]', **over)
        for over in ({}, {}, {"acceptingOrders": False})
    )
    events = [
        {
            "id": str(i),
            "slug": f"btc-updown-5m-{i}",
            "title": "Bitcoin Up or Down",
            "tags": up,
            "markets": [{k: v for k, v in row.items() if k not in ("tags", "events")}],
        }
        for i, row in enumerate((live, next_, shut))
    ]
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/events":
            return httpx.Response(200, json=events)
        return httpx.Response(200, json={"markets": []})

    gamma = httpx.Client(
        base_url="https://gamma.test", transport=httpx.MockTransport(handler)
    )
    before = int(time.time())
    admitted = walk_admissions(
        gamma, dataclasses.replace(POLICY, series_ids=(10684, 10192)), set()
    )

    assert [(m.condition, m.tags, m.event and m.event.slug) for m in admitted] == [
        (live["conditionId"], (("up-or-down", "Up or Down"),), "btc-updown-5m-0"),
        (next_["conditionId"], (("up-or-down", "Up or Down"),), "btc-updown-5m-1"),
    ]
    assert [r.url.path for r in seen] == ["/markets/keyset", "/events"]
    params = seen[1].url.params
    assert params.get_list("series_id") == ["10684", "10192"]
    assert (params["closed"], params["limit"]) == ("false", "100")
    start, end = (_iso_to_unix(params[k]) for k in ("end_date_min", "end_date_max"))
    assert before <= start <= before + 5 and end - start == 1800


def test_resolutions_keep_only_exact_payouts_in_one_call():
    conditions = [f"0x{i:064x}" for i in range(21)]
    rows = {
        conditions[0]: {"status": "resolved", "payouts": [1_000_000, 0]},
        conditions[1]: {"status": "resolved", "payouts": [0, 1_000_000]},
        conditions[2]: {"status": "resolved", "payouts": [500_000, 500_000]},
        conditions[3]: {"status": "resolved", "payouts": [700_000, 300_000]},
        conditions[4]: {"status": "resolved", "question_id": "0x1", "price": "0"},
        conditions[5]: {"status": "proposed", "payouts": [1_000_000, 0]},
        conditions[20]: {"status": "resolved", "payouts": [0, 1_000_000]},
    }
    asked: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/resolutions"
        batch = request.url.params["condition"].split(",")
        asked.append(batch)
        unasked = {"condition_id": "0x" + "f" * 64} | rows[conditions[0]]
        data = [{"condition_id": c} | rows[c] for c in batch if c in rows]
        return httpx.Response(200, json={"data": [*data, unasked]})

    found = resolutions(
        httpx.Client(transport=httpx.MockTransport(handler)), conditions
    )

    assert [len(batch) for batch in asked] == [21]
    assert found == {
        conditions[0]: (1, 0),
        conditions[1]: (0, 1),
        conditions[2]: (1, 1),
        conditions[20]: (0, 1),
    }


def test_a_failed_or_empty_resolutions_read_finds_nothing():
    failing = httpx.MockTransport(lambda request: httpx.Response(503))
    assert resolutions(httpx.Client(transport=failing), ["0x1"]) == {}
    assert resolutions(httpx.Client(transport=failing), []) == {}
