import secrets
from unittest.mock import patch

import pytest

from agentpit.common import check_state
from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.event import Event
from agentpit.datastructures.market import Market
from agentpit.db.table_read import TableRead
from agentpit.polymarket.conditional_token_framework import ConditionalTokenFramework
from agentpit.polymarket import polymarket_sync
from agentpit.polymarket.polymarket_sync import (
    POLYMARKET_GAMMA_URL,
    _is_market_over,
    _normalize_market_fields,
    _passes_market_filters,
    _polymarket_to_erc1155_tokens,
    build_create_market_request_from_json,
    create_polymarket_markets_if_needed,
    fetch_all_polymarket_markets,
    fetch_polymarket_market,
)
from tests.db_helpers import fresh_test_conn


@pytest.fixture()
def db():
    """Postgres test database connection with all tables created."""
    conn = fresh_test_conn()
    yield conn
    conn.close()


def test_sync_polymarket_markets_syncs_real_markets_to_db(db):
    """Test syncing live Polymarket markets into a local DB.

    Hits the real Polymarket API and mirrors each market onto the local
    CTF + Exchange — so anvil and the deployed exchange must be up.
    """
    from agentpit.config import Settings
    from agentpit.onchain.admin import OnchainAdmin
    from agentpit.onchain.contracts import Contracts
    from agentpit.onchain.deployment import Deployment
    from agentpit.onchain.web3_client import Web3Client

    settings = Settings()
    deployment = Deployment.load(settings.deployment_path)
    client = Web3Client(settings, deployment)
    admin = OnchainAdmin(client, Contracts(client.web3, deployment))

    # Capture a small, single-page trending set ONCE. `order=volume24hr` is a
    # live, churning feed, so re-fetching between syncs is non-deterministic
    # (the top-N shifts) — capture the upstream set and reuse it so the
    # idempotency check below is stable. A small cap also keeps the on-chain
    # prepareCondition work fast.
    pm_markets = fetch_all_polymarket_markets(
        order="volume24hr", max_markets=25, liquidity_threshold=0
    )
    created_markets = create_polymarket_markets_if_needed(db, pm_markets, admin)

    # We expect many markets to be created, but the exact number varies.
    assert len(created_markets) > 5

    db_markets, total = TableRead.list_markets(db, limit=len(created_markets) + 1)
    assert total == len(created_markets)
    assert len(db_markets) == len(created_markets)

    # list_markets returns newest-first (MARKET_ID DESC), which need not match
    # creation order, so match the synced row by id rather than by position.
    first_synced = created_markets[0]
    first_db = next(m for m in db_markets if m.market_id == first_synced.market_id)

    assert first_db.question == first_synced.question
    assert first_db.description == first_synced.description
    assert len(first_db.erc1155_tokens) > 0

    # Idempotent: re-syncing the SAME captured upstream set adds nothing.
    assert create_polymarket_markets_if_needed(db, pm_markets, admin) == []


def test_build_request_extracts_upstream_token_ids():
    pm_market = {
        "question": "Q",
        "description": "D",
        "id": 99,
        "conditionId": "0xcond",
        "slug": "q",
        "startDate": "2026-01-01T00:00:00Z",
        "endDate": "2026-12-31T00:00:00Z",
        "active": True,
        "closed": False,
        "tokens": [
            {"token_id": "777", "outcome": "Yes"},
            {"token_id": "888", "outcome": "No"},
        ],
    }
    req = build_create_market_request_from_json(pm_market)
    assert req.polymarket_yes_token_id == "777"
    assert req.polymarket_no_token_id == "888"


def test_fetch_all_polymarket_markets_requests_tags(monkeypatch):
    """Without include_tag=true every market comes back with `tags: null`."""
    from agentpit.polymarket import polymarket_sync

    seen: list[str] = []

    def fake_get(url: str):
        seen.append(url)
        return []

    monkeypatch.setattr(polymarket_sync, "get", fake_get)
    polymarket_sync.fetch_all_polymarket_markets(host="https://gamma.test")

    assert seen
    assert "include_tag=true" in seen[0]


# ----- a lapsed deadline is not the same as a finished market ----------------


def _overdue(**over):
    """A market whose stated end date passed two months ago."""
    m = {
        "conditionId": "0x" + "ab" * 32,
        "question": "Will the deadline slip again?",
        "endDate": "2026-06-01T00:00:00Z",
        "liquidity": "19002",
        "volumeNum": "76722445",
        "closed": False,
        "active": True,
        "archived": False,
        "acceptingOrders": True,
    }
    m.update(over)
    return m


def test_an_overdue_market_still_taking_orders_is_kept():
    """The Ethiopia case: endDate 2026-06-01, and $678k traded in the last 24
    hours. The deadline lapsed; the question did not."""
    m = _normalize_market_fields(_overdue())
    assert _is_market_over(m) is False


def test_an_overdue_market_no_longer_taking_orders_is_dropped():
    m = _normalize_market_fields(_overdue(acceptingOrders=False))
    assert _is_market_over(m) is True


def test_without_the_upstream_signal_the_date_still_decides():
    """Older Gamma shapes and fixtures carry no acceptingOrders. Falling back
    to the date keeps their behaviour rather than silently admitting them."""
    m = _overdue()
    del m["acceptingOrders"]
    m = _normalize_market_fields(m)
    assert _is_market_over(m) is True


def test_a_future_deadline_is_never_over_whatever_upstream_says():
    m = _normalize_market_fields(
        _overdue(endDate="2099-01-01T00:00:00Z", acceptingOrders=False)
    )
    assert _is_market_over(m) is False


def test_accepting_orders_is_coerced_from_its_string_forms():
    for raw, expected in (("true", True), ("false", False), (1, True), (0, False)):
        m = _normalize_market_fields(_overdue(acceptingOrders=raw))
        assert m["accepting_orders"] is expected, raw


# ----- an event is one question; half an answer is worse than none ----------


def _sib(name, *, v24, liq=20_000, closed=False, vol=1_000_000):
    """One outcome of a multi-outcome event.

    `vol` (cumulative volumeNum) defaults high so tests that only care about
    the `liq` knob aren't accidentally tripped by the filter's "stronger of
    liquidity or volume" rule. The illiquid-placeholder case below overrides
    it to 0 — a `Person C` nobody has ever traded has zero of both.
    """
    return {
        "conditionId": "0x" + name.encode().hex().ljust(64, "0")[:64],
        "question": f"Will {name} win?",
        "groupItemTitle": name,
        "volume24hr": v24,
        "volumeNum": vol,
        "liquidity": liq,
        "closed": closed,
        "active": True,
        "archived": False,
        "acceptingOrders": True,
        "endDate": "2099-01-01T00:00:00Z",
    }


def _event(*markets):
    return {
        "id": "77",
        "slug": "who-wins",
        "title": "Who wins?",
        "markets": list(markets),
    }


def _primary(name):
    """The market that qualified in the top-1000 window on its own merit."""
    m = _sib(name, v24=678_000)
    m["events"] = [{"id": "77"}]
    return m


def _fetcher_for(event):
    def fetch(ids, host):
        assert ids == ["77"], ids
        return [event]
    return fetch


def test_the_siblings_of_a_qualifying_market_come_with_it():
    favourite = _primary("Adanech")
    event = _event(favourite, _sib("Abiy", v24=328), _sib("Demeke", v24=1033))
    extra = polymarket_sync.fetch_event_siblings(
        [favourite], cap=12, liquidity_threshold=5000,
        fetcher=_fetcher_for(event),
    )
    assert sorted(m["groupItemTitle"] for m in extra) == ["Abiy", "Demeke"]


def test_the_market_that_already_qualified_is_not_returned_twice():
    favourite = _primary("Adanech")
    event = _event(favourite, _sib("Abiy", v24=328))
    extra = polymarket_sync.fetch_event_siblings(
        [favourite], cap=12, liquidity_threshold=5000,
        fetcher=_fetcher_for(event),
    )
    assert [m["groupItemTitle"] for m in extra] == ["Abiy"]


def test_the_cap_keeps_the_busiest_outcomes():
    favourite = _primary("Adanech")
    others = [_sib(f"P{i}", v24=100 - i) for i in range(10)]
    event = _event(favourite, *others)
    extra = polymarket_sync.fetch_event_siblings(
        [favourite], cap=3, liquidity_threshold=5000,
        fetcher=_fetcher_for(event),
    )
    # cap 3 covers the favourite plus the two busiest siblings.
    assert [m["groupItemTitle"] for m in extra] == ["P0", "P1"]


def test_an_illiquid_placeholder_sibling_is_dropped():
    """Upstream keeps zero-liquidity placeholders for unnamed candidates —
    `Person C`, `Person D`. Four of the Ethiopia event's 33 are exactly that."""
    favourite = _primary("Adanech")
    event = _event(favourite, _sib("Person C", v24=0, liq=0, vol=0))
    extra = polymarket_sync.fetch_event_siblings(
        [favourite], cap=12, liquidity_threshold=5000,
        fetcher=_fetcher_for(event),
    )
    assert extra == []


def test_a_closed_sibling_is_never_pulled():
    favourite = _primary("Adanech")
    event = _event(favourite, _sib("Gone", v24=5000, closed=True))
    extra = polymarket_sync.fetch_event_siblings(
        [favourite], cap=12, liquidity_threshold=5000,
        fetcher=_fetcher_for(event),
    )
    assert extra == []


def test_a_market_with_no_event_contributes_nothing():
    lone = _sib("Solo", v24=678_000)      # no "events" key at all
    assert polymarket_sync.fetch_event_siblings(
        [lone], cap=12, liquidity_threshold=5000,
        fetcher=lambda ids, host: pytest.fail("must not fetch"),
    ) == []


def test_siblings_carry_the_events_entry_they_came_from():
    """Markets nested under `/events` carry no `events` key of their own —
    verified 0 of 33 on the live Ethiopia event. Without attaching one here,
    every sibling lands as an orphan and gets wrapped in its own singleton
    event downstream instead of joining the group it actually belongs to."""
    favourite = _primary("Adanech")
    event = _event(favourite, _sib("Abiy", v24=328))
    extra = polymarket_sync.fetch_event_siblings(
        [favourite], cap=12, liquidity_threshold=5000,
        fetcher=_fetcher_for(event),
    )
    assert len(extra) == 1
    events = extra[0].get("events")
    assert events is not None
    assert events[0]["id"] == event["id"]
    assert events[0]["slug"] == event["slug"]


def test_a_sibling_survives_even_when_its_event_has_no_bindable_metadata():
    """Malformed upstream event (no title): tradeable beats grouped. The
    sibling is still returned; the startup orphan-wrap gives it a singleton
    event, same reasoning as pinned.py's sync_pinned_series."""
    favourite = _primary("Adanech")
    event = {
        "id": "77",
        "slug": "who-wins",
        # no "title" -> _event_entry can't build a bindable entry
        "markets": [favourite, _sib("Abiy", v24=328)],
    }
    extra = polymarket_sync.fetch_event_siblings(
        [favourite], cap=12, liquidity_threshold=5000,
        fetcher=_fetcher_for(event),
    )
    assert [m["groupItemTitle"] for m in extra] == ["Abiy"]
    assert "events" not in extra[0]


def test_the_cap_falls_back_to_lifetime_volume_when_24h_volume_is_missing():
    """volume24hr is frequently absent on markets nested under `/events`.
    `_as_float(None)` is 0.0, so without a fallback every such sibling ties
    and the cap keeps whichever upstream happened to list first — not the
    busiest. Lifetime volume (volumeNum) breaks the tie."""
    favourite = _primary("Adanech")
    quiet = _sib("Quiet", v24=0, vol=50)
    busy = _sib("Busy", v24=0, vol=999)
    del quiet["volume24hr"]
    del busy["volume24hr"]
    event = _event(favourite, quiet, busy)
    extra = polymarket_sync.fetch_event_siblings(
        [favourite], cap=2, liquidity_threshold=5000,
        fetcher=_fetcher_for(event),
    )
    # cap 2 covers the favourite plus the one busier by lifetime volume.
    assert [m["groupItemTitle"] for m in extra] == ["Busy"]


# ----- the house stops paying other people's gas -----------------------------


def test_the_redeem_loop_never_funds_gas():
    """Claiming a win costs the holder 91,743 gas. On a chain where that is
    money, it is theirs to spend — and we were sending a whole coin, 227x the
    need, before every single claim."""
    import inspect
    from agentpit.polymarket import polymarket_sync

    src = inspect.getsource(polymarket_sync.auto_redeem_resolved_markets)
    assert "fund_gas" not in src
    assert "gas_topup_wei" not in src


def test_the_redeem_loop_takes_no_gas_argument():
    import inspect
    from agentpit.polymarket import polymarket_sync

    params = inspect.signature(
        polymarket_sync.auto_redeem_resolved_markets
    ).parameters
    assert "gas_topup_wei" not in params


# ----- the series that regenerate faster than anyone reads them --------------


def _candidate(**over):
    """A market that clears every OTHER filter, so only the churn check can
    reject it.

    Built in camelCase and pushed through `_normalize_market_fields`, the way
    upstream payloads actually arrive: `_passes_market_filters` reads snake_case
    only, so this covers the normalization path too.
    """
    m = {
        "conditionId": "0x" + "cd" * 32,
        "question": "Highest temperature in Munich on June 10?",
        "endDate": "2099-01-01T00:00:00Z",
        "liquidity": "19002",
        "volumeNum": "76722445",
        "closed": False,
        "active": True,
        "archived": False,
        "acceptingOrders": True,
    }
    m.update(over)
    return m


def _kept(*, exclude_churn_series=True, excluded_categories=(), **over):
    """Does this market survive the catalogue filter?"""
    m = _normalize_market_fields(_candidate(**over))
    return _passes_market_filters(
        m,
        liquidity_threshold=0,
        closed=False,
        archived=False,
        exclude_churn_series=exclude_churn_series,
        excluded_categories=excluded_categories,
    )


def test_a_weather_market_with_no_tags_at_all_is_dropped_by_its_fee_type():
    """49 cities x ~3.4 thresholds = ~166 born every day, median life 55.9h:
    23% of every market we have ever created and resolved on chain.

    Markets nested under `/events` carry `feeType` but no `tags` key, so with
    nothing else to go on the fee type decides.
    """
    assert _kept(feeType="weather_fees") is False


def test_the_fee_type_alone_never_drops_a_tagged_weather_market():
    """`weather_fees` is upstream's schedule for the WHOLE weather / science /
    natural-disaster bucket, not a name for the daily-temperature series. Of
    158 rows carrying it in a live 2000-row sample, 5 are long-lived markets
    nothing like the churn series — dropping on the fee type alone thinned that
    category silently, with no error and no log line."""
    survivors = (
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
    for prop in ("spreads", "team_totals"):
        assert (
            _kept(feeType="sports_fees_v2", sportsMarketType=prop) is False
        ), prop


def test_the_game_itself_is_kept():
    """moneyline IS the game — the one sportsMarketType worth a condition."""
    assert _kept(feeType="sports_fees_v2", sportsMarketType="moneyline") is True


def test_a_market_carrying_neither_field_is_kept():
    """Older Gamma shapes and every fixture send neither field. Absence must
    never exclude: this drops only on positive evidence."""
    assert _kept() is True


def test_a_crypto_market_is_kept():
    """crypto_fees_v2 was 101 of a live 400-market sample and carries no
    sportsMarketType at all."""
    assert _kept(feeType="crypto_fees_v2") is True


def test_malformed_tags_never_raise():
    """`tags` is whatever upstream sent — null without include_tag=true, a bare
    string, entries that aren't dicts. A discovery filter that raises here would
    kill the whole sync pass."""
    for tags in (None, "daily-temperature", ["daily-temperature"], [None, 3]):
        assert _kept(tags=tags) is True, tags


def test_the_flag_switches_the_exclusion_back_off():
    """The operator can reverse the decision without a code change."""
    assert _kept(feeType="weather_fees", exclude_churn_series=False) is True


# ----- the predicate is only worth what the call sites do with it -----------


def _prop(name, *, v24, **over):
    """A sports prop sibling: a real Gamma payload shape, camelCase."""
    m = _sib(name, v24=v24)
    m["feeType"] = "sports_fees_v2"
    m["sportsMarketType"] = "spreads"
    m.update(over)
    return m


def test_a_prop_sibling_never_reaches_the_catalogue():
    """The sibling outcomes of a game event are exactly where the prop tail
    hangs, so the primary window dropping props is only half the job."""
    game = _primary("Game")
    event = _event(game, _prop("Spread", v24=5000))
    extra = polymarket_sync.fetch_event_siblings(
        [game], cap=12, liquidity_threshold=5000,
        fetcher=_fetcher_for(event),
    )
    assert extra == []


def test_the_sibling_pass_still_returns_props_when_the_flag_is_off():
    """Proves the assertion above is the flag working, not the fixture."""
    game = _primary("Game")
    event = _event(game, _prop("Spread", v24=5000))
    extra = polymarket_sync.fetch_event_siblings(
        [game], cap=12, liquidity_threshold=5000,
        fetcher=_fetcher_for(event),
        exclude_churn_series=False,
    )
    assert [m["groupItemTitle"] for m in extra] == ["Spread"]


def test_props_do_not_eat_the_cap_before_the_real_second_leg():
    """Every prop out-trades the second real leg of a game, so a cap applied
    before the churn filter is spent entirely on markets about to be dropped —
    the event contributes nothing and a genuine keeper is lost, which is worse
    than not excluding at all."""
    game = _primary("Game")
    props = [_prop(f"Spread{i}", v24=5000) for i in range(12)]
    second_leg = _sib("SecondLeg", v24=328)
    event = _event(game, *props, second_leg)
    extra = polymarket_sync.fetch_event_siblings(
        [game], cap=12, liquidity_threshold=5000,
        fetcher=_fetcher_for(event),
    )
    assert [m["groupItemTitle"] for m in extra] == ["SecondLeg"]


def _page_of(*markets):
    """A one-page Gamma `/markets` response followed by the empty page that
    stops pagination."""
    pages = [list(markets), []]

    def fake_get(url):
        return pages.pop(0) if pages else []

    return fake_get


def test_the_primary_window_drops_the_churn_series(monkeypatch):
    """`fetch_all_polymarket_markets` is the other half of the threading: the
    predicate cannot see its own call sites."""
    keeper = _sib("Game", v24=678_000)
    keeper["sportsMarketType"] = "moneyline"
    keeper["feeType"] = "sports_fees_v2"
    weather = _sib("Munich 30C", v24=19_000)
    weather["feeType"] = "weather_fees"
    monkeypatch.setattr(
        polymarket_sync, "get", _page_of(keeper, weather, _prop("Spread", v24=5000))
    )

    out = polymarket_sync.fetch_all_polymarket_markets(liquidity_threshold=5000)

    assert [m["groupItemTitle"] for m in out] == ["Game"]


def test_the_primary_window_keeps_everything_when_the_flag_is_off(monkeypatch):
    keeper = _sib("Game", v24=678_000)
    weather = _sib("Munich 30C", v24=19_000)
    weather["feeType"] = "weather_fees"
    monkeypatch.setattr(
        polymarket_sync, "get", _page_of(keeper, weather, _prop("Spread", v24=5000))
    )

    out = polymarket_sync.fetch_all_polymarket_markets(
        liquidity_threshold=5000, exclude_churn_series=False
    )

    assert sorted(m["groupItemTitle"] for m in out) == [
        "Game", "Munich 30C", "Spread",
    ]


def test_the_sync_entry_point_forwards_the_flag_to_both_passes(monkeypatch):
    """`fetch_and_sync_polymarket_markets` is where the Settings value lands;
    if it forwards a hardcoded False the whole feature is off in production."""
    seen = {}

    def fake_fetch_all(host, **kw):
        seen["primary"] = kw["exclude_churn_series"]
        return [{"condition_id": "0x01", "events": [{"id": "77"}]}]

    def fake_siblings(pm_markets, **kw):
        seen["siblings"] = kw["exclude_churn_series"]
        return []

    monkeypatch.setattr(
        polymarket_sync, "fetch_all_polymarket_markets", fake_fetch_all
    )
    monkeypatch.setattr(polymarket_sync, "fetch_event_siblings", fake_siblings)
    monkeypatch.setattr(
        polymarket_sync.TableWrite, "clear_price_changes", lambda db: None
    )
    monkeypatch.setattr(
        polymarket_sync, "create_polymarket_markets_if_needed",
        lambda db, pm_markets, admin: [],
    )

    for flag in (True, False):
        polymarket_sync.fetch_and_sync_polymarket_markets(
            db=None, admin=None, event_max_outcomes=12, exclude_churn_series=flag,
        )
        assert seen == {"primary": flag, "siblings": flag}, flag


# ----- categories the product does not carry at all --------------------------
#
# Distinct from the churn filter above: that one drops the prop tail and keeps
# the headline game, this one drops the whole category. Sports is excluded
# because the UI has no rendering for it — a match resolves in hours and its
# book empties the moment it does, leaving rows that read as broken.


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
    """`moneyline` is the one sportsMarketType the churn filter allows through
    — it is the game, not a prop. Excluding the category has to drop it anyway,
    or the filter removes the props and leaves exactly the rows complained
    about."""
    assert _kept(sportsMarketType="moneyline") is True
    assert _kept(sportsMarketType="moneyline", excluded_categories=["Sports"]) is False


def test_the_category_match_is_case_insensitive():
    """The setting is operator-typed; `resolve_category` returns "Sports"."""
    for spelling in ("sports", "SPORTS", "  Sports  "):
        assert (
            _kept(excluded_categories=[spelling], sportsMarketType="moneyline")
            is False
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
    """Restores the pre-2026-08-12 catalogue without a code change."""
    assert _kept(excluded_categories=[], sportsMarketType="moneyline") is True


def test_the_exclusion_is_independent_of_the_churn_flag():
    """Two separate decisions: someone turning the churn filter off to get the
    prop tail back must not silently re-admit a category the UI cannot draw."""
    assert (
        _kept(
            exclude_churn_series=False,
            excluded_categories=["Sports"],
            sportsMarketType="spreads",
        )
        is False
    )


# ----- refresh of known rows, 24 h change, start time ------------------------


def _fake_prepare(admin, question, labels):
    return ConditionId("0x" + secrets.token_hex(32)), [
        (str(int(secrets.token_hex(8), 16)), label) for label in labels
    ]


def _gamma_market(**over):
    tag = secrets.token_hex(4)
    pm = {
        "id": int(secrets.token_hex(4), 16),
        "conditionId": "0x" + secrets.token_hex(32),
        "question": f"Refresh {tag}?",
        "description": "d",
        "slug": f"refresh-{tag}",
        "startDate": "2026-01-01T00:00:00Z",
        "endDate": "2026-12-01T00:00:00Z",
        "active": True,
        "closed": False,
        "image": "https://img/old.png",
        "oneDayPriceChange": 0.04,
        "tokens": [
            {"token_id": str(int(secrets.token_hex(8), 16)), "outcome": "Yes"},
            {"token_id": str(int(secrets.token_hex(8), 16)), "outcome": "No"},
        ],
        "events": [
            {"id": f"ev-{tag}", "slug": f"event-{tag}", "title": "Old title"}
        ],
    }
    pm.update(over)
    return pm


def _sync(db, monkeypatch, pm_markets):
    monkeypatch.setattr(polymarket_sync, "prepare_market_on_chain", _fake_prepare)
    monkeypatch.setattr(
        polymarket_sync, "fetch_all_polymarket_markets", lambda host, **kw: pm_markets
    )
    polymarket_sync.fetch_and_sync_polymarket_markets(db, admin=None)


def _market_of(db, pm) -> Market:
    cid = TableRead.read_condition_id_by_polymarket_id(db, pm["id"])
    assert cid is not None
    market = TableRead.read_market_by_condition_id(db, cid)
    assert market is not None
    return market


def _event_of(db, pm) -> Event:
    event_id = _market_of(db, pm).event_id
    assert event_id is not None
    event = TableRead.get_event_by_id(db, event_id)
    assert event is not None
    return event


def test_a_known_market_and_event_pick_up_upstream_changes(db, monkeypatch):
    pm = _gamma_market()
    _sync(db, monkeypatch, [pm])
    before = _event_of(db, pm).event_id
    renamed = dict(
        pm,
        question="A reworded question?",
        slug="renamed-market",
        endDate="2026-12-31T00:00:00Z",
        image="https://img/new.png",
        events=[dict(pm["events"][0], slug="renamed-event", title="New title",
                     image="https://img/event.png")],
    )
    _sync(db, monkeypatch, [renamed])

    market = _market_of(db, pm)
    assert market.question == pm["question"]
    assert market.slug == "renamed-market"
    assert market.end_date == 1798675200
    assert market.icon_url == "https://img/new.png"
    assert market.url == "https://polymarket.com/market/renamed-market"
    event = _event_of(db, pm)
    assert (event.event_id, event.slug, event.title, event.icon_url) == (
        before, "renamed-event", "New title", "https://img/event.png"
    )
    assert event.url == "https://polymarket.com/event/renamed-event"


def test_an_event_slug_held_by_another_event_is_not_taken(db, monkeypatch):
    first, second = _gamma_market(), _gamma_market()
    _sync(db, monkeypatch, [first, second])
    taken = second["events"][0]["slug"]
    _sync(db, monkeypatch, [
        dict(first, events=[dict(first["events"][0], slug=taken, title="Moved")])
    ])

    event = _event_of(db, first)
    assert (event.slug, event.title) == (first["events"][0]["slug"], "Moved")


def test_the_24h_change_is_set_each_pass_and_cleared_when_unseen(db, monkeypatch):
    moving = _gamma_market(oneDayPriceChange=-0.07)
    flat = _gamma_market(oneDayPriceChange=None)
    _sync(db, monkeypatch, [moving, flat])
    assert _market_of(db, moving).price_change_24h == -0.07
    assert _market_of(db, flat).price_change_24h == 0.0

    _sync(db, monkeypatch, [dict(moving, oneDayPriceChange=0.02)])
    assert _market_of(db, moving).price_change_24h == 0.02
    assert _market_of(db, flat).price_change_24h is None


def test_the_event_start_time_wins_over_the_market_game_start_time():
    meta = polymarket_sync._extract_event_metadata({
        "gameStartTime": "2026-10-05 13:00:00+00",
        "gameId": "1711434",
        "events": [{
            "id": "1", "slug": "cs2", "title": "CS2",
            "startTime": "2026-10-05T14:00:00Z", "gameId": 1711434,
            "seriesSlug": "counter-strike",
        }],
    })
    assert meta is not None
    assert (meta["start_time"], meta["game_id"], meta["series_slug"]) == (
        1791208800, "1711434", "counter-strike"
    )


def test_the_start_time_falls_back_to_the_market_game_start_time():
    meta = polymarket_sync._extract_event_metadata({
        "gameStartTime": "2026-10-05 14:00:00+00",
        "gameId": "1711434",
        "events": [{"id": "1", "slug": "cs2", "title": "CS2"}],
    })
    assert meta is not None
    assert (meta["start_time"], meta["game_id"], meta["series_slug"]) == (
        1791208800, "1711434", None
    )


def test_a_sibling_event_entry_carries_the_game_fields():
    entry = polymarket_sync._event_entry({
        "id": "1", "slug": "cs2", "title": "CS2",
        "startTime": "2026-10-05T14:00:00Z", "gameId": 1711434,
        "seriesSlug": "counter-strike",
    })
    assert entry is not None
    assert (entry["startTime"], entry["gameId"], entry["seriesSlug"]) == (
        "2026-10-05T14:00:00Z", 1711434, "counter-strike"
    )


def test_the_sync_stores_the_game_fields_on_the_event(db, monkeypatch):
    pm = _gamma_market(gameStartTime="2026-10-05 14:00:00+00")
    pm["events"][0].update(gameId=19517, seriesSlug="nfl-2026")
    _sync(db, monkeypatch, [pm])

    event = _event_of(db, pm)
    assert (event.start_time, event.game_id, event.series_slug) == (
        1791208800, "19517", "nfl-2026"
    )
