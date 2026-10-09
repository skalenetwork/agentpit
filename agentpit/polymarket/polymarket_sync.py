"""
Utility to fetch all markets from Polymarket and re-create them locally
using TableWrite.create_market.
"""

import logging
import json
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from agentpit.common import check_state
from agentpit.config import Settings
from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.market_state import MarketState
from agentpit.domain.exceptions import (
    AdminGasPausedError,
    GasPriceMovedError,
    GasTopUpTimeoutError,
    InsufficientGasError,
    MarketStateError,
    NothingToClaimError,
    TransactionInProgressError,
    TransactionPendingError,
    TransactionRevertedError,
)
from agentpit.onchain.admin import OnchainAdmin
from agentpit.services.pending_user_txs import (
    in_flight_since,
    reconcile_pending_user_txs,
)
from agentpit.polymarket.category_resolver import category_rank, resolve_category
from agentpit.polymarket.tag_taxonomy import normalize_slug
from agentpit.datastructures.event import Event
from agentpit.polymarket.conditional_token_framework import ConditionalTokenFramework
from agentpit.services.market_service import prepare_market_on_chain, prepare_markets_on_chain
from agentpit.utils.parse import _iso_to_unix, hex2bytes
from py_clob_client.http_helpers.helpers import get

from agentpit.db.table_write import TableWrite
from agentpit.db.table_read import TableRead
from agentpit.datastructures.market import Market

logger = logging.getLogger(__name__)


# Silence noisy per-request INFO logs like:
# "httpx:_client.py:1026 HTTP Request: GET ... 'HTTP/2 200 OK'"
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


POLYMARKET_GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_MARKET_URL = "https://clob.polymarket.com/markets"


def _coalesce_key(market: dict, target: str, source_keys: list[str]) -> None:
    """Set market[target] from the first non-None source key if target is missing/None."""
    if market.get(target) is not None:
        return
    for key in source_keys:
        if market.get(key) is not None:
            market[target] = market[key]
            return


def _parse_list_field(raw: object) -> list:
    """Parse list-like API fields that may arrive as JSON strings."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, list) else []
        except (ValueError, TypeError):
            return []
    return []


def _ensure_tokens(market: dict) -> None:
    """
    Ensure market['tokens'] is present.
    Build it from clobTokenIds + outcomes when Gamma doesn't provide tokens.
    """
    if isinstance(market.get("tokens"), list) and len(market["tokens"]) > 0:
        return

    token_ids = _parse_list_field(
        market.get("clobTokenIds")
        if market.get("clobTokenIds") is not None
        else market.get("clobTokenids")
    )
    outcomes = _parse_list_field(market.get("outcomes"))

    if not token_ids:
        market["tokens"] = []
        return

    tokens = []
    for idx, token_id in enumerate(token_ids):
        if token_id is None:
            continue
        label = outcomes[idx] if idx < len(outcomes) else f"Outcome {idx + 1}"
        tokens.append({"token_id": str(token_id), "outcome": str(label)})
    market["tokens"] = tokens


def _as_float(value: object) -> float:
    """Coerce a Gamma numeric field (which arrives as int, float, str, or None) to float; 0.0 on failure."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def _as_optional_float(value: object) -> float | None:
    """A number, or None when upstream sent nothing usable.

    Distinct from `_as_float`, which answers 0.0: for a volume that is honest,
    but a liquidity of 0.0 asserts an empty order book and a competitive of
    0.0 asserts a settled market. None is the signal `update_event_metrics`
    skips on, so an unparseable payload leaves whatever was already stored.
    """
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _to_bool(value: object) -> bool | None:
    """Coerce common bool-like values; return None if unknown."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes"}:
            return True
        if text in {"false", "0", "no"}:
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return None


def _normalize_market_fields(market: dict) -> dict:
    """
    Normalize common Gamma market fields to the snake_case keys used by this module.
    """
    _coalesce_key(market, "condition_id", ["conditionId"])
    _coalesce_key(market, "question", ["title", "name"])
    _coalesce_key(market, "description", ["descriptionText"])
    _coalesce_key(market, "polymarket_id", ["id", "marketId"])
    _coalesce_key(
        market,
        "end_date_iso",
        ["endDateIso", "endDateISO", "endDate", "endTimeIso", "endTime"],
    )
    _coalesce_key(market, "active", ["isActive"])
    _coalesce_key(market, "closed", ["isClosed"])
    _coalesce_key(market, "archived", ["isArchived"])
    _coalesce_key(market, "liquidity", ["liquidityNum", "liquidityClob"])
    _coalesce_key(market, "accepting_orders", ["acceptingOrders"])
    # The two fields `_is_churn_series` reads. `_passes_market_filters` runs on
    # already-normalized markets and looks up snake_case keys only, so without
    # these the camelCase originals would be invisible to it and every churn
    # market would sail through.
    _coalesce_key(market, "fee_type", ["feeType"])
    _coalesce_key(market, "sports_market_type", ["sportsMarketType"])

    # Normalize bool-ish fields that may arrive as strings.
    for key in ("active", "closed", "archived", "accepting_orders"):
        coerced = _to_bool(market.get(key))
        if coerced is not None:
            market[key] = coerced

    # Ensure normalized source id is either int-like or None.
    pmid = market.get("polymarket_id")
    if pmid is not None:
        try:
            market["polymarket_id"] = int(pmid)
        except (TypeError, ValueError):
            market["polymarket_id"] = None

    _ensure_tokens(market)
    return market


def _is_market_over(market: dict) -> bool:
    """Is this market finished — as UPSTREAM sees it, not as its date claims?

    A stated end date is a deadline, not a verdict. Polymarket routinely lets a
    market trade past its own date while the question stays open: "Next Prime
    Minister of Ethiopia?" carried endDate 2026-06-01 and took $678k of volume
    in the 24 hours before this was written, ranking #5 of every active market.
    Trusting the date alone dropped 28 such markets out of the top-1000 window
    — $1.8M of daily volume, every one of them still accepting orders.

    So a lapsed date only counts when upstream has also stopped taking orders.
    When the payload carries no `accepting_orders` at all — older Gamma shapes,
    fixtures — the date decides, as it always did.
    """
    end_date_iso = market.get("end_date_iso")
    if not end_date_iso:
        return False
    try:
        # 'Z' is valid ISO 8601 but datetime.fromisoformat rejects it before 3.11.
        if end_date_iso.endswith("Z"):
            end_date_iso = end_date_iso[:-1] + "+00:00"
        end_date = datetime.fromisoformat(end_date_iso)
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False
    if end_date >= datetime.now(timezone.utc):
        return False
    accepting = market.get("accepting_orders")
    if accepting is None:
        return True
    return not accepting


#: Upstream's fee schedule for the WHOLE weather/science/natural-disaster
#: bucket -- NOT a name for the daily-temperature series. Of 158 rows carrying
#: it in a live 2000-row sample, 155 are the churn series and 5 are not:
#: "Hantavirus pandemic in 2026?" ($412k book, $17.7M lifetime volume, runs to
#: 2026-12-31), "Will any month of 2026 be the hottest on record?", two more
#: science/global-temp questions and "Will it rain during the Dutch Grand
#: Prix?". So this is a necessary condition, never a sufficient one.
WEATHER_FEE_TYPE = "weather_fees"
#: The tag that actually names the series. All 155 daily-temperature rows carry
#: it ([weather, recurring, hide-from-new, daily-temperature, munich, ...]);
#: none of the 5 long-lived weather_fees markets does.
WEATHER_TAG = "daily-temperature"
#: The ONLY sportsMarketType worth a condition on chain: the game itself.
HEADLINE_SPORTS_MARKET_TYPE = "moneyline"


def _tag_slugs(m: dict) -> set[str] | None:
    """The market's tag slugs, or None when the payload carries no usable tags.

    `tags` is whatever upstream sent: absent on markets nested under `/events`,
    None when `include_tag=true` was omitted, occasionally a JSON string, and
    its entries are not guaranteed to be dicts. A discovery filter must never
    raise on a malformed payload, so every shape that isn't a dict-with-a-slug
    is simply skipped -- and a payload that yields no slug at all is reported
    as None (unknown) rather than as an empty set (known to have no tags).
    """
    tags = m.get("tags")
    if not isinstance(tags, list):
        return None
    slugs = {
        tag["slug"]
        for tag in tags
        if isinstance(tag, dict) and isinstance(tag.get("slug"), str)
    }
    return slugs or None


def _is_churn_series(m: dict) -> bool:
    """Is this market part of a series that regenerates faster than it is read?

    Upstream fields decide it, and no slug is parsed: the `daily-temperature`
    tag names the temperature series, and `sportsMarketType` separates a game
    from the props hung off it. Both are absent from older Gamma shapes and from
    fixtures, and absence means KEEP -- this excludes only on positive evidence.

    `feeType == weather_fees` is NOT that evidence on its own. Upstream bills
    the entire weather/science/natural-disaster bucket on that schedule, so it
    also covers markets nothing like the churn series -- a $17.7M-volume
    hantavirus-pandemic question, "hottest month on record", VEI-4 eruption
    counts, rain at the Dutch Grand Prix, three of them running into 2027.
    Dropping on the fee type alone thinned that whole category silently. It is
    consulted only where the tag cannot be: markets nested under `/events`
    carry `feeType` but no `tags` key at all, and there the fee type is the one
    signal available, so weather + unknown tags still counts as the series.

    Measured on production: 49 cities x ~3.4 thresholds = ~166 daily temperature
    markets born every day with a median life of 55.9h -- 11% of the standing
    catalogue but 23% of every market ever created and resolved. Add the sports
    prop tail (spreads, totals, per-half, per-map, nrfi -- all hung off a game we
    already carry) and the two series are 89% of new creations, ~870M gas/day in
    prepareCondition + registerToken + splitPosition + reportPayouts.

    The gas is the measurable reason, not the first one. The product does not
    support these markets: a set handicap or a first-inning run has no reading
    on the site, so `atp-halys-kwon-2026-08-11-set-handicap-home-1pt5` arrives
    on the grid as a row nobody can act on. Excluding the tail leaves Sports a
    list of matches rather than a list of handicaps.

    A third consequence is repaired in passing. CONDITION_ID is derived from
    `keccak(question)`, and upstream reuses a prop's question text across games
    -- "Spread: Baltimore Orioles (-1.5)" is a new market every time the Orioles
    play, and the same condition forever to us. Production ran 251 UniqueViolation
    skips against 278 successful creations in three hours, and the stale row that
    squats on each reused string is a market from a game weeks past. Props ARE
    the reused strings; dropping them drops the collision with them.

    `moneyline` is an allow-list of one, deliberately: `sportsMarketType` is an
    open vocabulary upstream keeps extending (round_handicap_game_3 and
    both_teams_to_score_second_half are recent arrivals), so a deny-list would
    admit whatever prop type Polymarket invents next month and only exclude it
    once somebody noticed the gas bill. Allow-listing the game means a new prop
    type is excluded on arrival, and the cost of being wrong is one keeper
    missing until this constant grows -- not an unbounded new series on chain.
    """
    slugs = _tag_slugs(m)
    if slugs is not None and WEATHER_TAG in slugs:
        return True
    # No usable tags: fall back to the fee type, which on the sibling path is
    # all there is. Where tags DID arrive and did not say `daily-temperature`,
    # the market has answered the question already and the fee type is mute.
    if slugs is None and m.get("fee_type") == WEATHER_FEE_TYPE:
        return True
    sports_market_type = m.get("sports_market_type")
    if sports_market_type and sports_market_type != HEADLINE_SPORTS_MARKET_TYPE:
        return True
    return False


def _is_excluded_category(m: dict, excluded: "Iterable[str]") -> bool:
    """Is this market in a category the product does not carry at all?

    Different question from `_is_churn_series`, which drops the prop tail and
    keeps the headline game. This drops the category outright, so a market it
    rejects never reaches the chain, the catalogue, or the book mirror.

    Upstream fields decide it, as everywhere else in this module. `tags` is the
    general signal, reduced by the same `resolve_category` the sync stores on
    the event -- so what is excluded here is exactly what the UI would have
    labelled. `sportsMarketType` is consulted in addition, and it is not a
    nicety: an esports match arrives from Gamma with `tags: []`, so the tag
    path resolves nothing and the field is the ONLY evidence the market is
    sport (`cs2-mgc-mglz-2026-08-12`, the market this was built for). It is
    sound as a category test because upstream sets it on sports markets and on
    nothing else -- unlike `feeType`, whose weather bucket spans half a
    catalogue (see `_is_churn_series`).

    Absence of both signals means KEEP: this excludes only on positive
    evidence.
    """
    wanted = {c.strip().lower() for c in excluded if c and c.strip()}
    if not wanted:
        return False
    if "sports" in wanted and m.get("sports_market_type"):
        return True
    slugs = _tag_slugs(m)
    if slugs is None:
        return False
    category = resolve_category(slugs)
    return category is not None and category.lower() in wanted


def _passes_market_filters(
    m: dict,
    *,
    liquidity_threshold: float,
    closed: bool,
    archived: bool,
    exclude_churn_series: bool = True,
    excluded_categories: "Iterable[str]" = (),
) -> bool:
    """Does this ALREADY-NORMALIZED market belong in the catalogue?

    The single copy of that question. The primary window and the sibling pass
    both call it, so a market cannot be admitted through one path and rejected
    through the other.

    `exclude_churn_series` defaults to True precisely because both call sites go
    through here: a keyword with a default cannot be silently skipped by a call
    site that forgot it, which is the whole reason the check lives here and not
    in either caller.
    """
    if not m.get("condition_id"):
        return False
    if exclude_churn_series and _is_churn_series(m):
        return False
    # Deliberately NOT gated on `exclude_churn_series`: turning the churn
    # filter off to get the prop tail back must not re-admit a whole category
    # the UI cannot draw.
    if _is_excluded_category(m, excluded_categories):
        return False
    # Use the stronger of orderbook depth ("liquidity") and cumulative trade
    # volume ("volumeNum"). Multi-outcome favourites have shallow books on the
    # cheap side even though they're heavily traded — filtering on liquidity
    # alone silently drops exactly the markets users care about.
    liquidity = _as_float(m.get("liquidity"))
    volume = _as_float(m.get("volumeNum"))
    if volume == 0.0:
        volume = _as_float(m.get("volume"))
    if max(liquidity, volume) < liquidity_threshold:
        return False
    if not archived and m.get("archived", False):
        raise ValueError(
            f"API returned archived market {m.get('condition_id')} despite "
            "request for non-archived"
        )
    if not closed and m.get("closed", False):
        return False
    if not closed and _is_market_over(m):
        return False
    return True


def fetch_all_polymarket_markets(
    host: str = POLYMARKET_GAMMA_URL,
    closed: bool = False,
    active: bool = True,
    archived: bool = False,
    liquidity_threshold: float = 1000000,
    order: str | None = None,
    max_markets: int | None = None,
    exclude_churn_series: bool = True,
    excluded_categories: "Iterable[str]" = (),
) -> list[dict]:
    """
    Fetch all markets from Polymarket's Gamma API, paginating through all pages.

    Args:
        host: The Polymarket Gamma API base URL.
        closed: If True, include closed/resolved markets.
        active: If False, include inactive markets.
        archived: If True, include archived markets.
        exclude_churn_series: drop the daily-temperature and sports-prop series
            (see `_is_churn_series`). This module is called from library code
            and from tests, neither of which can reach Settings, so the value
            arrives as an argument — `AGENTPIT_SYNC_EXCLUDE_CHURN_SERIES` is
            read once in the API layer and threaded down.
        excluded_categories: drop these categories outright (see
            `_is_excluded_category`), threaded down the same way from
            `AGENTPIT_EXCLUDED_CATEGORIES`.

    Returns:
        A list of raw market dicts from the Polymarket API.
    """
    all_markets = []
    limit = 500
    offset = 0

    logger.info("Started fetching markets from Polymarket")

    # Build query parameters
    query_parts = [f"limit={limit}"]
    if archived:
        query_parts.append("archived=true")
    else:
        query_parts.append("archived=false")
    if not active:
        query_parts.append("active=false")
    else:
        query_parts.append("active=true")
    if closed:
        query_parts.append("closed=true")
    else:
        query_parts.append("closed=false")

    if order:
        query_parts.append(f"order={order}")
        query_parts.append("ascending=false")

    # Tags are the only live source of category on Gamma: the `category` field
    # is null for every market, and the nested `events[]` objects carry no tags
    # at all. Without this parameter each market comes back with `tags: null`.
    query_parts.append("include_tag=true")

    base_query = "&".join(query_parts)

    while True:
        response = get(f"{host}/markets?{base_query}&offset={offset}")

        # Gamma API returns a list of markets directly
        if isinstance(response, list):
            data = response
        else:
            data = []

        # Gamma caps each response at ~100 rows regardless of the requested
        # `limit`, so we paginate by the ACTUAL page size and stop on the first
        # empty page. (The old `len(data) < limit` check broke after one page
        # because 100 < 500 is always true.)
        if not data:
            break

        # Client-side filtering to match test expectations (tests/api/test_polymarket_sync.py)
        filtered_data = []
        for m in data:
            m = _normalize_market_fields(m)
            if _passes_market_filters(
                m,
                liquidity_threshold=liquidity_threshold,
                closed=closed,
                archived=archived,
                exclude_churn_series=exclude_churn_series,
                excluded_categories=excluded_categories,
            ):
                filtered_data.append(m)

        all_markets.extend(filtered_data)
        logger.debug(
            "Fetched %d markets (total so far: %d)", len(data), len(all_markets)
        )

        if max_markets is not None and len(all_markets) >= max_markets:
            break

        offset += len(data)

    if max_markets is not None:
        all_markets = all_markets[:max_markets]

    logger.info("Finished fetching %d markets from Polymarket", len(all_markets))
    return all_markets


#: Gamma caps a response at 100 rows; 40 ids per call leaves headroom.
_EVENT_BATCH = 40


def _fetch_events_by_id(ids: list[str], host: str) -> list[dict]:
    # Event ids are numeric strings from the payload we just fetched, and the
    # rest of this module builds Gamma URLs the same way (see
    # `fetch_polymarket_market`), so no escaping layer is introduced here.
    query = "&".join(f"id={i}" for i in ids)
    response = get(f"{host}/events?limit=100&{query}")
    return response if isinstance(response, list) else []


def _event_entry(src: dict) -> dict | None:
    """Build an ``events[]``-array entry (the shape `_extract_event_metadata`
    consumes) from an event-or-series dict. ``None`` if slug/title are missing.

    Lives here (not in `pinned.py`, which defined this first) because
    `pinned.py` already imports FROM this module — the reverse import would be
    circular. `pinned.py` imports this symbol from here instead.
    """
    slug = src.get("slug")
    title = src.get("title") or src.get("name")
    if not slug or not title:
        return None
    return {
        "id": src.get("id"),
        "slug": str(slug),
        "title": str(title),
        "description": src.get("description") or "",
        "image": src.get("image") or src.get("icon") or None,
        "icon": src.get("icon") or src.get("image") or None,
        "category": src.get("category"),
        "startDate": src.get("startDate") or src.get("startDateIso"),
        "endDate": src.get("endDate") or src.get("endDateIso"),
        "volume24hr": src.get("volume24hr"),
        "startTime": src.get("startTime"),
        "gameId": src.get("gameId"),
        "seriesSlug": src.get("seriesSlug"),
    }


def fetch_event_siblings(
    pm_markets: list[dict],
    *,
    cap: int,
    liquidity_threshold: float,
    host: str = POLYMARKET_GAMMA_URL,
    fetcher=None,
    exclude_churn_series: bool = True,
    excluded_categories: "Iterable[str]" = (),
) -> list[dict]:
    """The other outcomes of the events `pm_markets` belong to.

    An event is one question, and half an answer to it is worse than none: the
    top-1000-by-24h-volume window admitted exactly 1 of the 33 outcomes of
    "Next Prime Minister of Ethiopia?", so the site showed a $273M event as one
    candidate at under 1%.

    Keeps each event's `cap` busiest open outcomes by 24h volume (falling back
    to lifetime volume when 24h volume is absent, which upstream frequently
    leaves unset on markets nested under `/events`). The median event has 11,
    so 12 lets most through whole and truncates only the long-tail monsters —
    the largest upstream events carry 128 outcomes and nobody trades their
    tail.

    Markets nested under `/events` carry no `events[]` key of their own (only
    markets fetched through `/markets` do), so each returned sibling has one
    attached here from the event payload just fetched — otherwise it would
    land as an orphan and get wrapped in its own singleton event downstream,
    which is worse than not expanding at all. When the event itself carries no
    usable slug/title, the sibling is still returned (tradeable beats grouped;
    the orphan-wrap gives it a singleton, same reasoning as `pinned.py`).

    Returns only markets NOT already in `pm_markets`, so a market that
    qualified on its own merit can never be displaced by the cap. The sibling
    outcomes of a game event are exactly where the sports prop tail hangs, so
    `exclude_churn_series` is threaded through here too — the whole point of
    putting the check in `_passes_market_filters` is that this pass and the
    primary window answer the question identically.
    """
    fetch = fetcher or _fetch_events_by_id
    have = {m.get("condition_id") or m.get("conditionId") for m in pm_markets}
    event_ids: list[str] = []
    for m in pm_markets:
        for e in (m.get("events") or []):
            if e.get("id") is not None and str(e["id"]) not in event_ids:
                event_ids.append(str(e["id"]))
    if not event_ids:
        return []

    extra: list[dict] = []
    for i in range(0, len(event_ids), _EVENT_BATCH):
        try:
            events = fetch(event_ids[i : i + _EVENT_BATCH], host)
        except Exception as exc:  # one bad batch must not lose the rest
            logger.warning(
                "event sibling fetch failed for batch %d (%s)",
                i // _EVENT_BATCH,
                exc.__class__.__name__,
            )
            continue
        for event in events:
            group = _event_entry(event)
            if group is None:
                logger.warning(
                    "event sibling: event %s has no bindable metadata",
                    event.get("id"),
                )
            # Normalize a COPY — these dicts are nested inside the event
            # payload, and mutating them in place would corrupt it for any
            # other consumer of the same fetch.
            outcomes = [
                _normalize_market_fields(dict(raw))
                for raw in (event.get("markets") or [])
            ]
            # Normalizing before this check (rather than reading the raw
            # `closed` field) coerces string-ish values like "false" through
            # `_to_bool`, and matches the check `_passes_market_filters` makes
            # later — the raw field alone left a latent truthy-string trap.
            outcomes = [m for m in outcomes if not m.get("closed")]
            # Churn BEFORE the cap, not after. A prop-heavy game event carries
            # a dozen spreads/totals legs that each out-trade the second real
            # leg, so filtering after `outcomes[:cap]` let the props eat every
            # slot and then get dropped -- the event contributed nothing and
            # the genuine keeper was lost, which is worse than not excluding
            # at all. `_passes_market_filters` below still asks the same
            # question; asking it twice is free and keeps the chokepoint.
            if exclude_churn_series:
                outcomes = [m for m in outcomes if not _is_churn_series(m)]
            # Same reasoning, same place: an excluded category must not eat the
            # `outcomes[:cap]` slots and then be dropped, costing the event the
            # keepers it did have.
            outcomes = [
                m for m in outcomes
                if not _is_excluded_category(m, excluded_categories)
            ]
            outcomes.sort(
                key=lambda m: (
                    -_as_float(m.get("volume24hr")),
                    -_as_float(m.get("volumeNum")),
                )
            )
            for m in outcomes[:cap]:
                if m.get("condition_id") in have:
                    continue
                if not _passes_market_filters(
                    m,
                    liquidity_threshold=liquidity_threshold,
                    closed=False,
                    archived=False,
                    exclude_churn_series=exclude_churn_series,
                    excluded_categories=excluded_categories,
                ):
                    continue
                if group is not None:
                    m["events"] = [group]
                have.add(m["condition_id"])
                extra.append(m)
    return extra


def fetch_is_polymarket_market_closed(condition_id: ConditionId) -> bool:

    url = f"{CLOB_MARKET_URL}/{condition_id.value}"
    raw = get(url)
    logger.debug("CLOB single-market fetch raw result: %s", raw)

    # Normalize shape.
    market_raw: dict | None
    if isinstance(raw, dict):
        market_raw = raw
    elif isinstance(raw, list):
        # Pick the first dict whose conditionId/condition_id matches.
        market_raw = None
        for item in raw:
            if not isinstance(item, dict):
                continue
            normalized = _normalize_market_fields(item)
            cid = normalized.get("condition_id")
            if cid is not None and str(cid).lower() == condition_id.value.lower():
                market_raw = item
                break

    check_state(bool(market_raw))

    market = _normalize_market_fields(market_raw)

    cid = market.get("condition_id")
    check_state(cid is not None)
    check_state((cid).lower() == condition_id.value.lower())
    coerced = _to_bool(market.get("closed"))
    check_state(coerced is not None)
    return coerced


def fetch_polymarket_market(
    condition_id: ConditionId, host: str = POLYMARKET_GAMMA_URL
) -> dict | None:
    """
    Fetch a single market from Polymarket by condition_id using the Gamma API.

    Gamma API returns a list; we pick the first matching entry.
    """
    # Gamma expects the bare conditionId string
    url = f"{host}/markets?conditionId={condition_id.value}"
    result = get(url)
    logger.debug("Polymarket market fetch raw result: %s", result)

    # Gamma returns a list; guard for unexpected shapes
    markets: list[dict]
    if isinstance(result, list):
        markets = result
    elif isinstance(result, dict):
        # Some helpers might already unwrap a single result
        markets = [result]
    else:
        return None

    # Gamma *should* return one market, but in practice can return multiple.
    # We choose the first exact condition_id match after normalization.
    for raw in markets:
        if not isinstance(raw, dict):
            continue
        m = _normalize_market_fields(raw)
        cid = m.get("condition_id")
        if cid is None:
            continue
        if str(cid).lower() == condition_id.value.lower():
            return m

    # No matching market found
    if len(markets) > 1:
        logger.warning(
            "Gamma returned %d markets for conditionId=%s but none matched exactly",
            len(markets),
            condition_id.value,
        )
    return None


def _extract_event_metadata(pm_market: dict) -> dict | None:
    """Pull event fields from the upstream `events` array.

    Polymarket's Gamma response has ``events: [{id, slug, title, image, ...}]``.
    We take the first entry — markets that belong to more than one event are
    rare and we treat the first as canonical.

    The category is derived from the *market's* ``tags`` rather than from the
    nested event: Gamma's ``category`` field is null everywhere, and the nested
    event objects carry no tags.
    """
    events = pm_market.get("events")
    if not isinstance(events, list) or len(events) == 0:
        return None
    raw = events[0]
    if not isinstance(raw, dict):
        return None
    pm_event_id = raw.get("id")
    slug = raw.get("slug")
    title = raw.get("title") or raw.get("name")
    if not slug or not title:
        return None
    start_iso = raw.get("startDate") or raw.get("startDateIso")
    end_iso = raw.get("endDate") or raw.get("endDateIso")
    volume24hr = raw.get("volume24hr")
    volume = raw.get("volume")
    liquidity = raw.get("liquidity")
    competitive = raw.get("competitive")
    start_time = raw.get("startTime") or pm_market.get("gameStartTime")
    game_id = raw.get("gameId") or pm_market.get("gameId")
    series_slug = raw.get("seriesSlug")
    return {
        "polymarket_event_id": str(pm_event_id) if pm_event_id is not None else None,
        "slug": str(slug),
        "title": str(title),
        "description": str(raw.get("description") or ""),
        "icon_url": raw.get("image") or raw.get("icon"),
        # NOT raw.get("category") — that field is null on every Gamma response.
        # Tags live on the market, not on the nested event object. The slug
        # type check matters: a truthy non-str slug would reach .strip() inside
        # resolve_category and raise, permanently skipping this market on every
        # future sync pass.
        "category": resolve_category(
            t.get("slug")
            for t in (pm_market.get("tags") or [])
            if isinstance(t, dict)
            and isinstance(t.get("slug"), (str, type(None)))
        ),
        "start_date": _iso_to_unix(start_iso) if start_iso else None,
        "end_date": _iso_to_unix(end_iso) if end_iso else None,
        "volume_24hr": _as_float(volume24hr) if volume24hr is not None else None,
        "volume": _as_float(volume) if volume is not None else None,
        "liquidity": _as_optional_float(liquidity),
        "competitive": _as_optional_float(competitive),
        "start_time": _iso_to_unix(start_time) if start_time else None,
        "game_id": str(game_id) if game_id else None,
        "series_slug": str(series_slug) if series_slug else None,
    }


def _extract_outcome_metadata(pm_market: dict) -> tuple[str | None, str | None]:
    """Return ``(outcome_label, icon_url)`` for a single sub-market.

    Polymarket uses ``groupItemTitle`` for the short name shown inside an
    event (e.g. "France") and ``image`` for the per-outcome icon.
    """
    label = pm_market.get("groupItemTitle")
    icon = pm_market.get("image")
    return (str(label) if label else None, str(icon) if icon else None)


def _sync_event_category(db, event: Event, category: str | None) -> None:
    """Raise an event's category when ``category`` is stricter than what's stored.

    An event owns many markets and the sync's order across them isn't
    guaranteed, so last-writer-wins would let the category oscillate between
    passes. Comparing ranks makes the result order-independent: the event
    converges on the strictest category any member market resolves to.

    Never clears — a market whose tags resolve to nothing (~0.4% of the feed)
    must not undo a good categorization contributed by a sibling market.
    """
    if category is None:
        return
    if category_rank(category) >= category_rank(event.category):
        return
    TableWrite.update_event_category(db, event_id=event.event_id, category=category)


def bind_existing_market_to_upstream_event(
    db, *, polymarket_id: int, pm_market: dict
) -> Market | None:
    """Rebind an already-synced market to its upstream event.

    Used on every sync pass so markets created before this feature shipped
    (or markets whose upstream event was renamed/recategorized) get their
    event grouping refreshed. Returns the market when it is known, else None.
    """
    cid = TableRead.read_condition_id_by_polymarket_id(db, polymarket_id)
    if cid is None:
        return None
    market = TableRead.read_market_by_condition_id(db, cid)
    if market is None:
        return None
    bind_market_to_upstream_event(db, market, pm_market)
    return market


def extract_tags(pm_market: dict) -> list[tuple[str, str]] | None:
    """Pull ``(slug, label)`` pairs off an upstream market.

    Returns ``None`` — meaning "upstream said nothing, keep what is stored" —
    when ``tags`` is absent, null, or not a list. That distinction is the whole
    point: a Gamma request without ``include_tag=true`` returns ``tags: null``
    for every market, and treating that as an empty set would wipe good rows on
    every pass through such a code path. An empty LIST is different: upstream
    positively says this market has no tags, and the stored set should clear.

    Malformed entries are skipped individually rather than raised on. Raising
    here would abort this market's binding on every future pass, permanently.

    The label is only ever a display string, so a missing or non-string one
    falls back to the slug rather than dropping an otherwise good tag.
    """
    raw = pm_market.get("tags")
    if not isinstance(raw, list):
        return None
    out: list[tuple[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        slug = normalize_slug(entry.get("slug"))
        if slug is None:
            continue
        label = entry.get("label")
        out.append((slug, label if isinstance(label, str) and label.strip() else slug))
    return out


def bind_market_to_upstream_event(
    db, market: "Market", pm_market: dict
) -> None:
    """Idempotently upsert the upstream event and attach this market to it.

    No-op when the upstream market has no event metadata.
    """
    meta = _extract_event_metadata(pm_market)
    if meta is None:
        return
    # Prefer matching by polymarket_event_id (immutable) over slug (mutable).
    existing = None
    if meta["polymarket_event_id"]:
        existing = TableRead.get_event_by_polymarket_event_id(
            db, meta["polymarket_event_id"]
        )
    if existing is not None:
        # The upsert is skipped for known events (it matches on SLUG, which
        # upstream can rename), so the category is refreshed on its own.
        event = existing
        _sync_event_category(db, event, meta["category"])
    else:
        event = TableWrite.upsert_event(
            db,
            slug=meta["slug"],
            title=meta["title"],
            description=meta["description"],
            icon_url=meta["icon_url"],
            category=meta["category"],
            start_date=meta["start_date"],
            end_date=meta["end_date"],
            polymarket_event_id=meta["polymarket_event_id"],
        )
    TableWrite.refresh_event(
        db,
        event_id=event.event_id,
        slug=meta["slug"],
        title=meta["title"],
        icon_url=meta["icon_url"],
        end_date=meta["end_date"],
        start_time=meta["start_time"],
        game_id=meta["game_id"],
        series_slug=meta["series_slug"],
    )
    # Refresh upstream 24h volume on every pass (drives homepage order). No-op
    # when the upstream entry carried no volume, so a good value is never
    # clobbered with null.
    TableWrite.update_event_volume(
        db, event.event_id, meta["volume_24hr"], meta.get("volume")
    )
    TableWrite.update_event_metrics(
        db, event.event_id, meta["liquidity"], meta["competitive"]
    )
    outcome_label, icon_url = _extract_outcome_metadata(pm_market)
    TableWrite.attach_market_to_event(
        db,
        market_id=market.market_id,
        event_id=event.event_id,
        outcome_label=outcome_label,
        icon_url=icon_url,
    )
    # Mirror the upstream tag list. This is the same payload `resolve_category`
    # above collapses into one CATEGORY; storing it whole is what lets the
    # sidebar offer real subcategories. Skipped entirely when upstream carried
    # no tags list, so a caller without include_tag=true cannot clear good rows.
    tags = extract_tags(pm_market)
    if tags is not None:
        TableWrite.replace_market_tags(db, market_id=market.market_id, tags=tags)


def _polymarket_to_erc1155_tokens(pm_market: dict) -> list[tuple[str, str]]:
    """
    Convert a Polymarket market's token list into erc1155_tokens format:
    list of [token_id, label] pairs.

    Polymarket markets have a ``tokens`` field that is a list of dicts like:
        [{"token_id": "123...", "outcome": "Yes"}, ...]
    """
    tokens = pm_market.get("tokens", [])
    result = []
    for t in tokens:
        # Handle both snake_case (CLOB) and camelCase (Gamma) for token_id
        tid = t.get("token_id") or t.get("tokenId")
        outcome = t.get("outcome") or t.get("label") or "Unknown"
        if tid:
            result.append((tid, outcome))
    return result


def fetch_and_sync_polymarket_markets(
    db,
    admin: OnchainAdmin,
    host: str = POLYMARKET_GAMMA_URL,
    *,
    max_markets: int = 300,
    liquidity_min: float = 0.0,
    event_max_outcomes: int = 0,
    exclude_churn_series: bool = True,
    excluded_categories: "Iterable[str]" = (),
) -> list[Market]:
    """Discover + locally create the trending Polymarket markets.

    Fetches the top markets by 24h volume (descending) from Gamma and syncs any
    not already present onto the local CTF. Discovery only — resolution
    mirroring + auto-redeem run in their own loop.

    Args:
        max_markets: cap on how many top-by-volume markets to consider per pass.
        liquidity_min: minimum max(liquidity, volume) floor; 0 = no floor.
        event_max_outcomes: cap on sibling outcomes pulled in per event for any
            market that qualified in the primary window; 0 disables the pass
            (the default, so existing callers keep making no extra network
            calls).
        exclude_churn_series: forwarded to BOTH passes below, so the
            daily-temperature and sports-prop series are dropped whichever way
            a market would have entered. Comes from
            `Settings.sync_exclude_churn_series` at the API layer.
        excluded_categories: forwarded to both passes for the same reason.
            Comes from `Settings.excluded_categories`.
    """
    pm_markets = fetch_all_polymarket_markets(
        host,
        liquidity_threshold=liquidity_min,
        # Gamma's sort key is `volume24hr` (no underscore); `volume_24hr` is
        # rejected with HTTP 422 "order fields are not valid".
        order="volume24hr",
        max_markets=max_markets,
        exclude_churn_series=exclude_churn_series,
        excluded_categories=excluded_categories,
    )
    if event_max_outcomes > 0:
        siblings = fetch_event_siblings(
            pm_markets,
            cap=event_max_outcomes,
            liquidity_threshold=liquidity_min,
            host=host,
            exclude_churn_series=exclude_churn_series,
            excluded_categories=excluded_categories,
        )
        logger.info(
            "event expansion added %d sibling markets to %d primary",
            len(siblings), len(pm_markets),
        )
        pm_markets = pm_markets + siblings
    TableWrite.clear_price_changes(db)
    created_markets = create_polymarket_markets_if_needed(db, pm_markets, admin)
    return created_markets


# Markets per chain step when no admin says otherwise (offline tests).
_DEFAULT_CHAIN_CHUNK = 32


def create_polymarket_markets_if_needed(
    db,
    pm_markets: list[dict],
    admin: OnchainAdmin,
) -> list[Any]:
    """Mirror every not-yet-synced market onto the local chain and database.

    Three steps, so a chunk of new markets shares one or two blocks instead of
    two blocks each: classify every candidate (known ones only get refreshed),
    prepare the new ones on chain a chunk at a time with every transaction
    broadcast before any receipt is awaited, then insert what succeeded. A
    market listed twice counts once. A chunk whose chain step raises as a
    whole, or whose sends found the node out of reach or no admin slot free
    (the result's `stop`), ends the chain work of the pass in one warning
    line: what that chunk prepared is inserted, the rest count as failed,
    and the next pass retries them.

    Each database step runs in its own SAVEPOINT: the batch shares one
    transaction (the caller's db.write()), and without it a single failed
    INSERT (e.g. duplicate question -> same derived CONDITION_ID) aborts the
    transaction — every later market dies with InFailedSqlTransaction and the
    closing COMMIT silently becomes a ROLLBACK, losing the entire batch.
    """
    created_markets: list[Market] = []
    failed = 0

    new: list[tuple[CreateMarketRequest, dict]] = []
    seen: set[str] = set()
    for pm_market in pm_markets:
        question = pm_market.get("question") or "<no question>"
        pm_id = pm_market.get("id")
        if pm_id is not None:
            if str(pm_id) in seen:
                # Gamma's pagination over a live volume24hr sort can return a
                # market twice. Nothing is inserted before the chain step, so
                # a second copy would be classified new as well and then fail
                # on the unique polymarket_id; the first copy stands for both.
                logger.debug(
                    "Skip %r: polymarket id %s twice in one pass", question, pm_id
                )
                continue
            seen.add(str(pm_id))
        try:
            with db.transaction():
                request = _refresh_known_or_build_request(db, pm_market)
        except Exception as exc:
            failed += 1
            _log_skip(question, exc)
            continue
        if request is not None:
            new.append((request, pm_market))

    chunk = admin.sync_chunk_size if admin is not None else _DEFAULT_CHAIN_CHUNK
    for start in range(0, len(new), chunk):
        batch = new[start : start + chunk]
        try:
            prepared = prepare_markets_on_chain(
                admin,
                [(r.question, [label for _, label in r.erc1155_tokens]) for r, _ in batch],
            )
        except Exception as exc:
            # The whole step failed: a chain read raised, the node being out
            # of reach, say (a market's own trouble comes back as its result,
            # never raised). The chunks after it would likely fail the same
            # way, after sending transactions whose answers are lost, each
            # one a nonce gap. So the chain work of this pass ends here, in
            # one line; the next pass retries every one of these markets.
            left = len(new) - start
            failed += left
            _log_chain_stop(exc, left)
            break
        # A plain list (a test's stand-in) carries no stop.
        stop = getattr(prepared, "stop", None)
        chain_failed = 0  # markets of this chunk the chain step failed
        for (request, pm_market), outcome in zip(batch, prepared, strict=True):
            if isinstance(outcome, Exception):
                failed += 1
                if stop is None:
                    _log_skip(request.question, outcome)
                else:
                    chain_failed += 1  # told in the one line below
                    logger.debug("Skip %r details", request.question, exc_info=outcome)
                continue
            try:
                with db.transaction():
                    market = _insert_prepared_market(db, request, pm_market, outcome)
            except Exception as exc:
                failed += 1
                _log_skip(request.question, exc)
                continue
            created_markets.append(market)
        if stop is not None:
            # The chunk came back, but its sends found the node out of reach
            # or no admin slot free: the next chunks would only add sends
            # whose answers are lost (each one a nonce for the stall healer)
            # or wait out the same timeout. Same ending as a raise.
            unsent = len(new) - start - len(batch)
            failed += unsent
            _log_chain_stop(stop, chain_failed + unsent)
            break

    logger.info(
        "Synced %d/%d Polymarket markets locally (%d failed)",
        len(created_markets),
        len(pm_markets),
        failed,
    )
    return created_markets


def _log_chain_stop(exc: Exception, left: int) -> None:
    logger.warning(
        "Chain step failed (%s: %s); %d new markets left for the next pass",
        exc.__class__.__name__,
        _one_line(exc),
        left,
    )
    logger.debug("Chain step failure details", exc_info=exc)


def _log_skip(question: str, exc: Exception) -> None:
    # One bad market (e.g. an RPC blip) shouldn't kill the whole sync batch.
    # Keep the message single-line; the stack trace lives at debug level for
    # when you actually want it. A MarketStateError's text says what is
    # missing on chain, which is the reason to read the line at all.
    if isinstance(exc, MarketStateError):
        logger.warning(
            "Skip %r (%s: %s)", question, exc.__class__.__name__, _one_line(exc)
        )
    else:
        logger.warning("Skip %r (%s)", question, exc.__class__.__name__)
    logger.debug("Skip %r details", question, exc_info=exc)


def _one_line(exc: BaseException, limit: int = 300) -> str:
    text = " ".join(str(exc).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def create_polygon_market_if_does_not_exist(
    db,
    pm_market: dict,
    admin: OnchainAdmin,
) -> Market | None:
    """The single-market form of the sync: None when already synced."""
    request = _refresh_known_or_build_request(db, pm_market)
    if request is None:
        return None
    outcome_labels = [label for _, label in request.erc1155_tokens]
    prepared = prepare_market_on_chain(admin, request.question, outcome_labels)
    return _insert_prepared_market(db, request, pm_market, prepared)


def _refresh_known_or_build_request(db, pm_market: dict) -> CreateMarketRequest | None:
    """None for a market already synced (after refreshing it), else the request
    to create it with."""
    request = build_create_market_request_from_json(pm_market)
    check_state(bool(request.polymarket_id))
    assert request.polymarket_id is not None  # narrowed by check_state above

    # Cheap path first: a market already synced for this polymarket_id needs no
    # on-chain prepare — just keep its event grouping current and return.
    known = bind_existing_market_to_upstream_event(
        db, polymarket_id=request.polymarket_id, pm_market=pm_market
    )
    if known is None:
        return request
    _refresh_market(db, known.market_id, request, pm_market)
    # Backfill the upstream token-id cross-reference for markets synced
    # before positional capture existed (Up/Down windows had null ids), so
    # the book mirror can resolve them. No-op once populated.
    TableWrite.update_market_polymarket_tokens(
        db,
        polymarket_id=request.polymarket_id,
        yes_token_id=request.polymarket_yes_token_id,
        no_token_id=request.polymarket_no_token_id,
    )
    return None


def _insert_prepared_market(
    db,
    request: CreateMarketRequest,
    pm_market: dict,
    prepared: tuple[ConditionId, list[tuple[str, str]]],
) -> Market:
    # The upstream conditionId/tokenIds are replaced by the locally derived
    # ones; polymarket_id stays as the cross-reference.
    request.condition_id, request.erc1155_tokens = prepared
    market = TableWrite.create_market(db, request, True)
    bind_market_to_upstream_event(db, market, pm_market)
    _refresh_market(db, market.market_id, request, pm_market)
    logger.info("Added market: %s", request.question)
    return market


def _refresh_market(
    db, market_id: int, request: CreateMarketRequest, pm_market: dict
) -> None:
    TableWrite.refresh_market(
        db,
        market_id=market_id,
        slug=request.slug,
        end_date=request.end_date,
        icon_url=request.icon_url,
        price_change_24h=_as_float(pm_market.get("oneDayPriceChange")),
    )


def _default_resolution_fetcher(polymarket_condition_id: str) -> dict | None:
    """Look up upstream Polymarket market by conditionId via the CLOB API.

    CLOB is the canonical single-document endpoint and uses Polymarket's
    immutable Polygon-mainnet conditionId — not Gamma's mutable integer id.
    """
    url = f"{CLOB_MARKET_URL}/{polymarket_condition_id}"
    raw = get(url)
    if isinstance(raw, list):
        return raw[0] if raw else None
    if isinstance(raw, dict):
        return raw
    return None


def _winner_index_if_resolved(pm_response: dict) -> int | None:
    """Return the resolved outcome index, or None if upstream isn't settled."""
    if not pm_response.get("closed"):
        return None
    # umaResolutionStatus is the canonical "UMA has settled" flag, but some
    # markets close via CLOB-only resolution. We require closed + a winner
    # marker on a token; we don't insist on UMA settlement specifically.
    tokens = pm_response.get("tokens") or []
    for idx, t in enumerate(tokens):
        if isinstance(t, dict) and t.get("winner") is True:
            return idx
    return None


def list_scan_candidates(db, *, after_market_id: int, limit: int) -> list:
    """One slice of the rotating resolution scan. See TableRead.list_unresolved_markets_after."""
    return TableRead.list_unresolved_markets_after(db, after_market_id, limit)


def mirror_polymarket_resolutions(
    db,
    admin: OnchainAdmin,
    *,
    fetcher=_default_resolution_fetcher,
    now: int,
    market_ids: "set[int] | None" = None,
    candidates: "list | None" = None,
) -> int:
    """Walk synced markets and mirror upstream resolutions onto the local CTF.

    For each ACTIVE/CLOSED market with a `polymarket_id`, fetches upstream
    state. When upstream is settled with a clear winner, calls
    `admin.report_payouts` against the local CTF, then flips the local row
    to RESOLVED. Idempotent on subsequent runs.

    `market_ids`, when given, restricts the pass to those market ids — used by
    the fast per-window resolve loop to check only a couple of just-ended
    pinned windows instead of scanning (and upstream-fetching) every market.

    `candidates`, when given, replaces the end-date query entirely — that is how
    the rotating scan feeds in markets whose stated end date has not arrived but
    which upstream has already closed. Widening the candidate set cannot resolve
    anything extra on its own: `_winner_index_if_resolved` still requires the
    upstream document to say `closed` with a winning token.

    Returns the count of markets newly resolved this pass.
    """
    from eth_utils.crypto import keccak  # local import: avoid circulars at module load

    resolved_count = 0
    if candidates is None:
        candidates = TableRead.list_unresolved_ended_markets(db, now)
    if market_ids is not None:
        candidates = [m for m in candidates if m.market_id in market_ids]
    for market in candidates:
        if market.polymarket_condition_id is None:
            # Synced before the upstream-conditionId column existed, or a
            # locally-authored market with no Polymarket linkage. Either way,
            # nothing for the upstream mirror to do.
            continue
        if market.market_state in {MarketState.RESOLVED, MarketState.CANCELLED}:
            continue
        try:
            pm_response = fetcher(market.polymarket_condition_id)
        except Exception as exc:
            logger.warning(
                "resolution fetch failed for %s (%s)",
                market.market_id,
                exc.__class__.__name__,
            )
            continue
        if pm_response is None:
            continue
        slug = pm_response.get("market_slug")
        if isinstance(slug, str) and slug and slug != market.slug:
            TableWrite.update_market_slug(db, market.market_id, slug)
        winner_idx = _winner_index_if_resolved(pm_response)
        if winner_idx is None:
            continue

        # Binary YES/NO payout vector. partition is [1<<i for i in range(2)],
        # so payouts align: index 0 = YES, index 1 = NO.
        payouts = [
            1 if i == winner_idx else 0 for i in range(len(market.erc1155_tokens))
        ]
        question_id = keccak(text=market.question)
        cond_bytes = bytes.fromhex(market.condition_id.value[2:])

        # Check on-chain idempotency: if the CTF already has payouts, skip
        # the tx but still flip the DB row so local state catches up.
        denom = admin._contracts.ctf.functions.payoutDenominator(
            cond_bytes
        ).call()  # noqa: SLF001
        if denom == 0:
            try:
                admin.report_payouts(question_id, payouts)
            except Exception as exc:
                logger.warning(
                    "reportPayouts failed for market %s: %s",
                    market.market_id,
                    exc,
                )
                continue

        try:
            TableWrite.resolve_market(
                db,
                market_id=market.market_id,
                winning_outcome_index=winner_idx,
            )
            resolved_count += 1
        except ValueError as exc:
            # Race: another caller flipped the row between our read and write.
            logger.info("resolve_market noop for %s: %s", market.market_id, exc)
    return resolved_count


# A claim that failed is not retried for a while, so a failure that repeats
# cannot hold up the rest of the pass. Keyed by (user_id, market_id); the value
# is the time.monotonic() it may go again. Module-level because a pass builds
# its services afresh every time. Only `auto_redeem_resolved_markets` touches
# it, and app.py runs that under `_redeem_lock`, so one thread at a time.
#
# A claim that mined and reverted costs gas every time and should not happen
# after the on-chain gate: left alone for an hour.
_REVERT_BACKOFF_SECONDS = 3600
# A claim that was refused or could not go out (the gas breaker is paused, the
# wallet could not be funded, the top-up timed out, or something unexpected)
# may well go out soon, and costs little to try again: a quarter of an hour.
# Without it the same first `auto_redeem_max_per_pass` holders in key order
# would spend the cap on every pass, with the breaker paused or the kill switch
# off, and the rest -- the house's own claims among them -- would never be
# reached.
_REFUSED_BACKOFF_SECONDS = 900
_claim_backoff_until: dict[tuple[str, int], float] = {}


def _claimable_payout(balances: list[int], den: int, nums: list[int]) -> int:
    """What `redeemPositions` would pay for these balances: each outcome's
    stake times its numerator over the denominator, floored per outcome the
    way the CTF floors it. Losing tokens (numerator 0) count for nothing."""
    if den == 0:
        return 0
    return sum(bal * num // den for bal, num in zip(balances, nums))


def auto_redeem_resolved_markets(
    db, admin: OnchainAdmin, settings: Settings
) -> int:
    """Claim for every holder owed a claim on each RESOLVED, not-yet-fully-
    redeemed market.

    `db` is a DbSession (not a raw connection) because PositionService manages
    its own read/write connections. For each market the on-chain payout vector
    is read once, and each participant's balances once (trades + split/merge,
    including the house bot). A participant is claimed for when they are a bot
    or opted in (AUTO_REDEEM_ENABLED), and `redeemPositions` would pay them at
    least the claim minimum (`AGENTPIT_MIN_CLAIM_MICRO`, read through the
    sponsor so the claim gate uses the same number).

    Every claim goes through `UserGasSponsor`, which tops the holder up to
    exactly what that one claim needs right before sending it. A wallet with
    no native coin at all is claimed for like any other, and a holder owed
    less than the minimum -- nothing, only losing tokens, or dust -- is never
    sent anything, so they cost the admin no gas.

    FULLY_REDEEMED is set once no participant is owed the minimum, so dust and
    losing tokens no longer hold a market open. A holder who opted out and is
    owed it still does: the flag only stops this scan, and they may switch the
    toggle back on later. Bots are claimed for regardless of the flag: a bot
    has no one to ask for consent and no interface to ask from, and the
    house's own accounts (e.g. the liquidity mirror) would otherwise hold every
    resolved market open forever.

    A pass makes at most `AGENTPIT_AUTO_REDEEM_MAX_PER_PASS` claim attempts,
    then returns; the market it stopped in, and every one after it, stay open
    for the next pass. Attempts rather than successes, because a refused claim
    has already spent reads and an estimate, and a reverted one spent gas.
    Each claim takes about two blocks while the pass holds `_redeem_lock`,
    which both resolution loops wait on.

    The pass first settles the user transactions whose outcome nobody saw
    (`reconcile_pending_user_txs`): a split that mined unseen becomes the
    SPLIT row that makes its holder a participant, and a claim that mined
    unseen its REDEEM row, before the scan reads anyone's balance. A failure
    there is logged and the pass goes on.

    A holder whose lock is held (they are claiming by hand) is skipped for
    this pass. So is one with a transaction on the market still in flight (a
    pending row younger than its TTL that the reconciler could not settle
    yet): a claim now would only meet the duplicate guard's 409. Neither is
    backed off or counted toward the cap, and both keep the market open; a
    holder in flight holds it open even before any trade or SPLIT row names
    them. A claim whose own outcome turns out unknown
    (`TransactionPendingError`) keeps the market open too, without a backoff:
    its pending row keeps the holder out until it is settled.

    One whose top-up the gas breaker refused or timed out, or whose claim
    the node refused twice as the fee rose (`GasPriceMovedError`), or whose
    dry wallet the sponsor would not fund (kill switch off), or whose claim
    failed unexpectedly, is left alone for `_REFUSED_BACKOFF_SECONDS`; one
    whose claim mined and reverted for an hour. The backoff keeps a failure
    that repeats from spending the cap on the same holders every pass. A
    holder in backoff is passed by without counting toward the cap. The
    expected refusals are logged without a traceback.

    A market whose chain reads fail (a bad row, an RPC error) is logged and
    left open; the pass goes on to the next one.

    Returns the number of holder redemptions performed.
    """
    from agentpit.services.gas_sponsor import UserGasSponsor
    from agentpit.services.position_service import PositionService

    sponsor = UserGasSponsor(db, admin, settings)
    svc = PositionService(db, admin, sponsor)
    minimum = sponsor.min_claim_micro
    cap = settings.auto_redeem_max_per_pass
    now = time.monotonic()
    for expired in [k for k, until in _claim_backoff_until.items() if until <= now]:
        del _claim_backoff_until[expired]

    def back_off(key: tuple[str, int], seconds: int) -> None:
        _claim_backoff_until[key] = time.monotonic() + seconds

    try:
        reconcile_pending_user_txs(db, admin)
    except Exception:
        logger.exception(
            "auto-redeem: pending user transactions could not be settled; "
            "tried again next pass"
        )

    redeemed = 0
    attempts = 0
    with db.read() as conn:
        markets = TableRead.list_resolved_unredeemed_markets(conn)
        cutoff = in_flight_since(int(time.time()))
        # (api_key, market_id) of every transaction still on its way.
        in_flight = {
            (row.api_key, row.market_id)
            for row in TableRead.list_pending_user_txs(conn)
            if row.created_at >= cutoff
        }

    for market in markets:
        try:
            token_strs = [t for t, _ in market.erc1155_tokens]
            token_ints = [int(t) for t in token_strs]
            with db.read() as conn:
                api_keys = TableRead.list_participant_api_keys_for_market(
                    conn, market.market_id, token_strs
                )
            api_keys |= {k for k, m in in_flight if m == market.market_id}
            vector = None
            if api_keys:
                vector = admin.payout_vector(
                    hex2bytes(market.condition_id.value), len(token_ints)
                )
        except Exception:
            # One bad row or one RPC error must not stop every later market
            # from being claimed. It stays open and is read again next pass.
            logger.exception(
                "auto-redeem: market %s could not be read; retried next pass",
                market.market_id,
            )
            continue
        if vector is None:
            # Nobody ever traded or split it, so nobody can be owed anything.
            with db.write() as conn:
                TableWrite.mark_fully_redeemed(conn, market.market_id)
            continue

        den, nums = vector
        if den == 0:
            # RESOLVED here without a reportPayouts on chain (the admin resolve
            # route does exactly that): nobody can be paid, so nobody is
            # settled either. One read per pass until the payout lands.
            logger.debug(
                "auto-redeem: market %s has no payout on chain yet",
                market.market_id,
            )
            continue

        still_owed = False
        # Sorted so a capped pass takes holders in the same order every time.
        for api_key in sorted(api_keys):
            with db.read() as conn:
                user = TableRead.get_user_by_api_key(conn, api_key)
            if user is None:
                continue
            if (api_key, market.market_id) in in_flight:
                logger.info(
                    "auto-redeem: %s has a transaction on market %s whose "
                    "outcome is not known yet; retried next pass",
                    user.eth_address,
                    market.market_id,
                )
                still_owed = True
                continue
            try:
                balances = admin.ctf_balances(user.eth_address, token_ints)
            except Exception:
                # Probably the node, not the holder: stop reading this market
                # for this pass instead of one traceback per holder.
                logger.exception(
                    "auto-redeem: balances of %s on market %s could not be "
                    "read; the market is retried next pass",
                    user.eth_address,
                    market.market_id,
                )
                still_owed = True
                break
            payout = _claimable_payout(balances, den, nums)
            # `payout <= 0` on its own, not left to the minimum: `Settings`
            # refuses a minimum below 1, but a claim that pays nothing is
            # pure admin gas even if that were ever relaxed.
            if payout <= 0 or payout < minimum:
                # Nothing, only the losing side, or dust: a claim would be
                # pure admin gas, and nothing here is worth keeping open for.
                continue
            if not (user.is_bot or user.auto_redeem):
                # Opted out: the claim is theirs to make, and the market stays
                # open in case they switch the toggle back on.
                still_owed = True
                continue
            key = (user.user_id, market.market_id)
            if _claim_backoff_until.get(key, 0.0) > now:
                still_owed = True
                continue
            if attempts >= cap:
                logger.info(
                    "auto-redeem: %d claims this pass; market %s onwards waits "
                    "for the next",
                    cap,
                    market.market_id,
                )
                return redeemed
            attempts += 1
            try:
                svc.redeem(user, market.market_id, payout_vector=vector)
                redeemed += 1
            except NothingToClaimError:
                # The gate re-reads under the holder's lock and found less
                # than this scan did: they claimed by hand in between.
                logger.info(
                    "auto-redeem: %s already claimed market %s",
                    user.eth_address,
                    market.market_id,
                )
            except TransactionInProgressError:
                # Refused at the holder's lock, before any chain read or any
                # gas: nothing was spent, so the attempt is given back. Counted,
                # `cap` such holders visited first in key order would use the
                # cap up on every pass -- a held lock is cheap to keep -- and
                # the honest holders after them would never be reached.
                attempts -= 1
                logger.info(
                    "auto-redeem: %s has a transaction in progress; market %s "
                    "is retried next pass",
                    user.eth_address,
                    market.market_id,
                )
                still_owed = True
            except TransactionPendingError as exc:
                logger.warning(
                    "auto-redeem: claim for %s on market %s was sent and its "
                    "outcome is unknown (%s); settled by a later pass",
                    user.eth_address,
                    market.market_id,
                    exc,
                )
                still_owed = True
            except (
                AdminGasPausedError,
                InsufficientGasError,
                GasTopUpTimeoutError,
                GasPriceMovedError,
            ) as exc:
                back_off(key, _REFUSED_BACKOFF_SECONDS)
                logger.warning(
                    "auto-redeem: claim for %s on market %s not sent (%s); "
                    "not retried for %d s",
                    user.eth_address,
                    market.market_id,
                    exc,
                    _REFUSED_BACKOFF_SECONDS,
                )
                still_owed = True
            except TransactionRevertedError as exc:
                back_off(key, _REVERT_BACKOFF_SECONDS)
                logger.warning(
                    "auto-redeem: claim for %s on market %s reverted (%s); "
                    "not retried for %d s",
                    user.eth_address,
                    market.market_id,
                    exc,
                    _REVERT_BACKOFF_SECONDS,
                )
                still_owed = True
            except Exception:
                back_off(key, _REFUSED_BACKOFF_SECONDS)
                logger.exception(
                    "auto-redeem failed for %s on market %s; not retried "
                    "for %d s",
                    user.eth_address,
                    market.market_id,
                    _REFUSED_BACKOFF_SECONDS,
                )
                still_owed = True

        if not still_owed:
            with db.write() as conn:
                TableWrite.mark_fully_redeemed(conn, market.market_id)

    return redeemed


def sync_market_state(db, condition_id: ConditionId) -> None:
    """Placeholder until upstream→local resolution mirroring is built.

    The original implementation queried Polymarket's CLOB for "is closed"
    and the CTF for resolution payouts. After the local-mirror change,
    `condition_id` here is the *local* one — Polymarket has never seen it,
    so the CLOB call 404s. Resolution propagation needs its own design:
    fetch upstream state by `polymarket_id`, then have the local admin
    `reportPayouts` against the local CTF before flipping the row to
    RESOLVED.
    """
    return None


def _token_id_of(t: object) -> str | None:
    """Pull a token id from a Polymarket tokens-list entry (snake or camel)."""
    if not isinstance(t, dict):
        return None
    tid = t.get("token_id") or t.get("tokenId")
    return str(tid) if tid is not None else None


def _extract_yes_no_token_ids(pm_market: dict) -> tuple[str | None, str | None]:
    """Return (yes_token_id, no_token_id) from a Polymarket market's tokens list.

    Polymarket binary markets list two tokens. Yes/No markets are matched by
    label (case-insensitive). For binary markets whose outcomes aren't literally
    Yes/No (e.g. *Up/Down*), fall back to **positional** mapping — slot 0 is the
    yes-side, slot 1 the no-side — which is the same index convention used by the
    erc1155 token order and the resolution payout vector. This positional id is
    what lets the book mirror resolve an upstream token for these markets
    (the mirror skips any market with a null yes-token). Returns (None, None)
    for non-binary markets.
    """
    tokens = pm_market.get("tokens") or []
    yes_id: str | None = None
    no_id: str | None = None
    for t in tokens:
        if not isinstance(t, dict):
            continue
        tid = _token_id_of(t)
        outcome = (t.get("outcome") or t.get("label") or "").strip().lower()
        if tid is None:
            continue
        if outcome == "yes":
            yes_id = tid
        elif outcome == "no":
            no_id = tid

    if (yes_id is None or no_id is None) and len(tokens) == 2:
        if yes_id is None:
            yes_id = _token_id_of(tokens[0])
        if no_id is None:
            no_id = _token_id_of(tokens[1])
    return yes_id, no_id


def build_create_market_request_from_json(pm_market: dict) -> CreateMarketRequest:
    question = pm_market.get("question", "").strip()
    description = pm_market.get("description", "").strip()
    polymarket_id = pm_market.get("id")
    erc1155_tokens = _polymarket_to_erc1155_tokens(pm_market)
    yes_tok, no_tok = _extract_yes_no_token_ids(pm_market)
    slug = pm_market.get("slug")
    start_date = pm_market.get("startDate")
    end_date = pm_market.get("endDate")
    active = pm_market.get("active")
    closed = pm_market.get("closed")
    condition_id = pm_market.get("conditionId")
    outcome_label, icon_url = _extract_outcome_metadata(pm_market)

    if active and not closed:
        state = MarketState.ACTIVE
    else:
        state = MarketState.CLOSED

    request = CreateMarketRequest(
        question=question,
        description=description,
        polymarket_id=polymarket_id,
        polymarket_condition_id=condition_id,
        polymarket_yes_token_id=yes_tok,
        polymarket_no_token_id=no_tok,
        erc1155_tokens=erc1155_tokens,
        slug=slug,
        start_date=_iso_to_unix(start_date),
        end_date=_iso_to_unix(end_date) if end_date is not None else None,
        state=state,
        condition_id=ConditionId(condition_id),
        outcome_label=outcome_label,
        icon_url=icon_url,
    )
    return request


