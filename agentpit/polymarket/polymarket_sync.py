import json
import logging
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import NamedTuple, Self

import httpx

from agentpit.config import Settings
from agentpit.datastructures.board import WireTeam
from agentpit.datastructures.market import Payouts
from agentpit.polymarket.category_resolver import category_rank, resolve_category
from agentpit.polymarket.gamma import _iso
from agentpit.polymarket.tag_taxonomy import normalize_slug
from agentpit.datastructures.event import Event
from agentpit.utils.parse import _iso_to_unix

from agentpit.db.session import DbSession
from agentpit.db.table_write import TableWrite
from agentpit.db.table_read import TableRead

logger = logging.getLogger(__name__)


# Silence noisy per-request INFO logs like:
# "httpx:_client.py:1026 HTTP Request: GET ... 'HTTP/2 200 OK'"
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


POLYMARKET_GAMMA_URL = "https://gamma-api.polymarket.com"
DATA_API_URL = "https://data-api.polymarket.com"
SERIES_LEAD_SECONDS = 1800
RESOLUTION_BATCH = 20
_CONDITION = re.compile(r"0x[0-9a-f]{64}")
_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
_GENERIC_LOGOS = frozenset({"a.png", "ufc.png"})


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


def _as_optional_float(value: object) -> float | None:
    """A number, or None when upstream sent nothing usable.

    None is the signal `update_event_metrics` skips on, so an unparseable
    payload leaves whatever was already stored.
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


def _unix(value: object) -> int | None:
    try:
        return _iso_to_unix(value) if isinstance(value, str) and value else None
    except ValueError:
        return None


#: The tag that actually names the series. All 155 daily-temperature rows carry
#: it ([weather, recurring, hide-from-new, daily-temperature, munich, ...]);
#: none of the 5 long-lived weather_fees markets does.
WEATHER_TAG = "daily-temperature"
#: The game itself: the only sportsMarketType the depth walk admits.
HEADLINE_SPORTS_MARKET_TYPE = "moneyline"
#: Sports types the churn rule keeps: none, the game, per-game and per-map winners.
KEPT_SPORTS_MARKET_TYPES: frozenset[str | None] = frozenset(
    {None, HEADLINE_SPORTS_MARKET_TYPE, "child_moneyline"}
)
_PAYOUTS: dict[tuple[str, ...], Payouts] = {
    ("1", "0"): (1, 0),
    ("0", "1"): (0, 1),
    ("0.5", "0.5"): (1, 1),
}
_MICRO_PAYOUTS: dict[tuple[int, ...], Payouts] = {
    (1_000_000, 0): (1, 0),
    (0, 1_000_000): (0, 1),
    (500_000, 500_000): (1, 1),
}


class Verdict(StrEnum):
    CARRY = "carry"
    MALFORMED = "malformed"
    NOT_BINARY = "not_binary"
    NOT_V1 = "not_v1"
    NOT_TRADING = "not_trading"
    LOW_VOLUME = "low_volume"
    THIN_BOOK = "thin_book"
    CHURN = "churn"
    EXCLUDED = "excluded"


@dataclass(frozen=True, slots=True)
class UpstreamEvent:
    polymarket_event_id: str | None
    slug: str
    title: str
    description: str
    icon_url: str | None
    start_date: int | None
    end_date: int | None
    volume_24hr: float | None
    volume: float | None
    liquidity: float | None
    competitive: float | None
    start_time: int | None
    game_id: str | None
    series_slug: str | None


@dataclass(frozen=True, slots=True)
class UpstreamMarket:
    pm_id: int
    condition: str
    tokens: tuple[str, str]
    labels: tuple[str, str]
    question: str
    description: str
    slug: str
    start_date: int
    end_date: int | None
    label: str | None
    icon: str | None
    closed: bool | None
    accepting: bool | None
    active: bool | None
    payouts: Payouts | None
    volume_24hr: float
    volume: float
    liquidity: float
    price_change_24h: float | None
    sports_market_type: str | None
    tags: tuple[tuple[str, str], ...]
    event: UpstreamEvent | None

    @property
    def category(self) -> str | None:
        return resolve_category(slug for slug, _ in self.tags)


@dataclass(frozen=True, slots=True)
class CoveragePolicy:
    min_volume_24h: float
    exclude_churn: bool
    excluded_categories: frozenset[str]
    excluded_tags: frozenset[str]
    game_tag_ids: tuple[int, ...]
    min_game_liquidity: float
    series_ids: tuple[int, ...] = ()
    event_outcomes: int = 12

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(
            settings.sync_min_volume_24h,
            settings.sync_exclude_churn_series,
            frozenset(
                c.strip().lower() for c in settings.excluded_categories if c.strip()
            ),
            frozenset(t.strip().lower() for t in settings.excluded_tags if t.strip()),
            tuple(settings.sync_game_tag_ids),
            settings.sync_min_game_liquidity,
            tuple(settings.sync_series_ids),
        )

    def admits(self, m: UpstreamMarket) -> Verdict:
        if m.volume_24hr < self.min_volume_24h:
            return Verdict.LOW_VOLUME
        return self._screen(m)

    def admits_game(self, m: UpstreamMarket) -> Verdict:
        if (
            m.sports_market_type != HEADLINE_SPORTS_MARKET_TYPE
            or m.liquidity < self.min_game_liquidity
        ):
            return Verdict.THIN_BOOK
        return self._screen(m)

    def _screen(self, m: UpstreamMarket) -> Verdict:
        if m.closed is not False or m.accepting is not True or m.active is False:
            return Verdict.NOT_TRADING
        slugs = {slug for slug, _ in m.tags}
        if self.exclude_churn and (
            WEATHER_TAG in slugs or m.sports_market_type not in KEPT_SPORTS_MARKET_TYPES
        ):
            return Verdict.CHURN
        if (
            slugs & self.excluded_tags
            or (m.category or "").lower() in self.excluded_categories
            or (
                m.sports_market_type is not None
                and "sports" in self.excluded_categories
            )
        ):
            return Verdict.EXCLUDED
        return Verdict.CARRY


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


def extract_tags(pm_market: dict) -> list[tuple[str, str]] | None:
    """Pull (slug, label) pairs off an upstream market; a missing or non-list
    `tags` returns None and parse rejects the row."""
    raw = pm_market.get("tags")
    if not isinstance(raw, list):
        return None
    labels: dict[str, str | None] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        slug = normalize_slug(entry.get("slug"))
        if slug is None:
            continue
        label = entry.get("label")
        if labels.get(slug) is None:
            labels[slug] = label if isinstance(label, str) and label.strip() else None
    return [(slug, label or slug) for slug, label in labels.items()]


def _extract_event_metadata(pm_market: dict) -> UpstreamEvent | None:
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
    game_id = raw.get("gameId") or pm_market.get("gameId")
    series_slug = raw.get("seriesSlug")
    return UpstreamEvent(
        polymarket_event_id=str(pm_event_id) if pm_event_id is not None else None,
        slug=str(slug),
        title=str(title),
        description=str(raw.get("description") or ""),
        icon_url=raw.get("image") or raw.get("icon"),
        start_date=_unix(raw.get("startDate") or raw.get("startDateIso")),
        end_date=_unix(raw.get("endDate") or raw.get("endDateIso")),
        volume_24hr=_as_optional_float(raw.get("volume24hr", 0)),
        volume=_as_optional_float(raw.get("volume")),
        liquidity=_as_optional_float(raw.get("liquidity")),
        competitive=_as_optional_float(raw.get("competitive")),
        start_time=_unix(
            raw.get("startTime")
            or pm_market.get("gameStartTime")
            or pm_market.get("eventStartTime")
        ),
        game_id=str(game_id) if game_id else None,
        series_slug=str(series_slug) if series_slug else None,
    )


def parse(row: object) -> UpstreamMarket | Verdict:
    if not isinstance(row, dict):
        return Verdict.MALFORMED
    if row.get("version") != "v1":
        return Verdict.NOT_V1
    tokens = _parse_list_field(row.get("clobTokenIds"))
    labels = _parse_list_field(row.get("outcomes"))
    if len(tokens) != 2 or len(labels) != 2:
        return Verdict.NOT_BINARY
    tags = extract_tags(row)
    label, icon = _extract_outcome_metadata(row)
    closed = _to_bool(row.get("closed"))
    resolved = closed is True and row.get("umaResolutionStatus") == "resolved"
    prices = tuple(str(p) for p in _parse_list_field(row.get("outcomePrices")))
    try:
        m = UpstreamMarket(
            pm_id=int(row["id"]),
            condition=row["conditionId"].lower(),
            tokens=(str(int(tokens[0])), str(int(tokens[1]))),
            labels=(str(labels[0]), str(labels[1])),
            question=row["question"].strip(),
            description=row["description"].strip(),
            slug=row["slug"].strip(),
            start_date=_iso_to_unix(row["startDate"]),
            end_date=_iso_to_unix(row["endDate"]) if row.get("endDate") else None,
            label=label,
            icon=icon,
            closed=closed,
            accepting=_to_bool(row.get("acceptingOrders")),
            active=_to_bool(row.get("active")),
            payouts=_PAYOUTS.get(prices) if resolved else None,
            volume_24hr=_as_optional_float(row.get("volume24hr")) or 0.0,
            volume=_as_optional_float(row.get("volumeNum")) or 0.0,
            liquidity=_as_optional_float(row.get("liquidityNum")) or 0.0,
            price_change_24h=_as_optional_float(row.get("oneDayPriceChange", 0)),
            sports_market_type=row.get("sportsMarketType") or None,
            tags=tuple(tags or ()),
            event=_extract_event_metadata(row),
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        return Verdict.MALFORMED
    if (
        tags is None
        or not _CONDITION.fullmatch(m.condition)
        or not (m.question and m.description and m.slug)
        or (m.end_date is not None and m.end_date <= m.start_date)
    ):
        return Verdict.MALFORMED
    if resolved and m.payouts is None:
        logger.warning(
            "Polymarket market %s resolved at outcomePrices %s, not an exact payout vector",
            m.condition,
            list(prices),
        )
    return m


def _pages(
    gamma: httpx.Client, params: dict[str, str | int | float]
) -> Iterator[list[dict]]:
    params = {"limit": 100, "closed": "false", "include_tag": "true", **params}
    while True:
        page = gamma.get("/markets/keyset", params={**params, "_cb": time.time_ns()})
        body = page.raise_for_status().json()
        if body["markets"]:
            yield body["markets"]
        if not body["markets"] or "next_cursor" not in body:
            return
        params["after_cursor"] = body["next_cursor"]


def _admitted(
    rows: list[dict], admits: Callable[[UpstreamMarket], Verdict]
) -> list[UpstreamMarket]:
    return [
        m
        for row in rows
        if isinstance(m := parse(row), UpstreamMarket) and admits(m) is Verdict.CARRY
    ]


def walk_admissions(
    gamma: httpx.Client, policy: CoveragePolicy, carried: set[str]
) -> list[UpstreamMarket]:
    admitted: list[UpstreamMarket] = []
    for rows in _pages(
        gamma, {"order": "volume24hr", "ascending": "false", "archived": "false"}
    ):
        admitted += _admitted(rows, policy.admits)
        if (
            _as_optional_float(rows[-1].get("volume24hr")) or 0.0
        ) < policy.min_volume_24h:
            break
    for tag_id in policy.game_tag_ids:
        for rows in _pages(
            gamma,
            {
                "tag_id": tag_id,
                "sports_market_types": HEADLINE_SPORTS_MARKET_TYPE,
                "liquidity_num_min": policy.min_game_liquidity,
            },
        ):
            admitted += _admitted(rows, policy.admits_game)
    admitted += _siblings(gamma, policy, admitted, carried)
    if not policy.series_ids:
        return admitted
    now = int(time.time())
    events = (
        gamma.get(
            "/events",
            params={
                "series_id": list(policy.series_ids),
                "closed": "false",
                "end_date_min": _iso(now),
                "end_date_max": _iso(now + SERIES_LEAD_SECONDS),
                "limit": 100,
                "_cb": time.time_ns(),
            },
        )
        .raise_for_status()
        .json()
    )
    return admitted + [
        m
        for event in events
        for row in event.get("markets") or []
        if isinstance(
            m := parse(row | {"tags": event.get("tags"), "events": [event]}),
            UpstreamMarket,
        )
        and policy._screen(m) is Verdict.CARRY
    ]


def _siblings(
    gamma: httpx.Client,
    policy: CoveragePolicy,
    admitted: list[UpstreamMarket],
    carried: set[str],
) -> list[UpstreamMarket]:
    have = carried | {m.condition for m in admitted}
    ids = list(
        dict.fromkeys(
            m.event.polymarket_event_id
            for m in admitted
            if m.condition not in carried
            and m.event
            and m.event.polymarket_event_id
            and m.event.game_id is None
        )
    )
    chosen: list[str] = []
    for start in range(0, len(ids), EVENT_BATCH):
        try:
            events = (
                gamma.get(
                    "/events",
                    params={
                        "id": ids[start : start + EVENT_BATCH],
                        "limit": EVENT_BATCH,
                        "_cb": time.time_ns(),
                    },
                )
                .raise_for_status()
                .json()
            )
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Gamma events batch failed (%s)", exc.__class__.__name__)
            continue
        for event in events:
            outcomes = [
                m
                for row in event.get("markets") or []
                if isinstance(
                    m := parse(row | {"tags": event.get("tags")}), UpstreamMarket
                )
                and policy._screen(m) is Verdict.CARRY
            ]
            outcomes.sort(key=lambda m: (m.volume_24hr, m.volume), reverse=True)
            chosen += [
                m.condition
                for m in outcomes[: policy.event_outcomes]
                if m.condition not in have
            ]
    return [
        m
        for m in fetch_markets(gamma, chosen).values()
        if policy._screen(m) is Verdict.CARRY
    ]


EVENT_BATCH = 40
GAMMA_BATCH = 50


def _team(label: str, event: dict) -> WireTeam | None:
    team = next(
        (
            t
            for t in event.get("teams") or []
            if isinstance(t, dict) and label in (t.get("alias"), t.get("name"))
        ),
        None,
    )
    if team is None:
        return None
    logo = str(team.get("logo") or "")
    record = str(team.get("record") or "")
    color = str(team.get("color") or "")
    return WireTeam(
        logo=(
            None
            if logo in ("", event.get("image"))
            or logo.rsplit("/", 1)[-1] in _GENERIC_LOGOS
            else logo
        ),
        record=(
            None
            if set(record) <= {"0", "-"}
            else record.removesuffix("-0") if record.count("-") == 2 else record
        ),
        color=color if _COLOR.fullmatch(color) else None,
        abbr=str(team.get("abbreviation") or "").upper().rstrip("0123456789") or None,
    )


def attach_teams(db: DbSession, gamma: httpx.Client) -> None:
    with db.read() as conn:
        games = TableRead.games_without_teams(conn)
    ids = list(games)
    for start in range(0, len(ids), EVENT_BATCH):
        try:
            events = (
                gamma.get(
                    "/events",
                    params={
                        "id": ids[start : start + EVENT_BATCH],
                        "limit": EVENT_BATCH,
                        "_cb": time.time_ns(),
                    },
                )
                .raise_for_status()
                .json()
            )
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("Gamma teams batch failed (%s)", exc.__class__.__name__)
            continue
        rows = [
            (games[pm][0], label, _team(label, event))
            for event in events
            if (pm := str(event.get("id"))) in games
            for label in games[pm][1]
        ]
        with db.write() as conn:
            TableWrite.insert_outcome_teams(conn, rows)


CLOB_URL = "https://clob.polymarket.com"


class Clob(NamedTuple):
    accepting: bool
    game_start: int | None


def clob_market(http: httpx.Client, condition_id: str) -> Clob | None:
    try:
        response = http.get(f"{CLOB_URL}/clob-markets/{condition_id}")
        body = response.json()
        if (
            response.status_code != 200
            or str(body.get("c")).lower() != condition_id.lower()
        ):
            return None
        game_start = body.get("gst") if body.get("cbos") is True else None
        return Clob(
            body.get("ao") is True, _iso_to_unix(game_start) if game_start else None
        )
    except (httpx.HTTPError, ValueError, AttributeError):
        return None


def fetch_markets(
    http: httpx.Client, condition_ids: list[str]
) -> dict[str, UpstreamMarket]:
    found: dict[str, UpstreamMarket] = {}
    for closed in ("false", "true"):
        missing = [c for c in condition_ids if c not in found]
        for start in range(0, len(missing), GAMMA_BATCH):
            params = {
                "condition_ids": missing[start : start + GAMMA_BATCH],
                "limit": GAMMA_BATCH,
                "closed": closed,
                "include_tag": "true",
                "_cb": time.time_ns(),
            }
            try:
                response = http.get(f"{POLYMARKET_GAMMA_URL}/markets", params=params)
                response.raise_for_status()
                rows = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning(
                    "Gamma markets batch failed (%s)", exc.__class__.__name__
                )
                continue
            for row in rows:
                if isinstance(m := parse(row), UpstreamMarket):
                    found[m.condition] = m
    return found


def resolutions(http: httpx.Client, conditions: list[str]) -> dict[str, Payouts]:
    if not conditions:
        return {}
    try:
        rows = (
            http.get(
                f"{DATA_API_URL}/v2/resolutions",
                params={"condition": ",".join(conditions)},
            )
            .raise_for_status()
            .json()["data"]
        )
    except (httpx.HTTPError, ValueError, KeyError) as exc:
        logger.warning("Data API resolutions failed (%s)", exc.__class__.__name__)
        return {}
    return {
        condition: payouts
        for row in rows
        if (condition := row.get("condition_id")) in conditions
        and row.get("status") == "resolved"
        and (payouts := _MICRO_PAYOUTS.get(tuple(row.get("payouts") or ())))
    }


def bind_market_to_upstream_event(db, market_id: int, m: UpstreamMarket) -> None:
    meta = m.event
    if meta is None:
        return
    # Prefer matching by polymarket_event_id (immutable) over slug (mutable).
    existing = None
    if meta.polymarket_event_id:
        existing = TableRead.get_event_by_polymarket_event_id(
            db, meta.polymarket_event_id
        )
    if existing is not None:
        # The upsert is skipped for known events (it matches on SLUG, which
        # upstream can rename), so the category is refreshed on its own.
        event = existing
        _sync_event_category(db, event, m.category)
    else:
        event = TableWrite.upsert_event(
            db,
            slug=meta.slug,
            title=meta.title,
            description=meta.description,
            icon_url=meta.icon_url,
            category=m.category,
            start_date=meta.start_date,
            end_date=meta.end_date,
            polymarket_event_id=meta.polymarket_event_id,
        )
    TableWrite.refresh_event(
        db,
        event_id=event.event_id,
        slug=meta.slug,
        title=meta.title,
        icon_url=meta.icon_url,
        end_date=meta.end_date,
        start_time=meta.start_time,
        game_id=meta.game_id,
        series_slug=meta.series_slug,
    )
    TableWrite.update_event_volume(db, event.event_id, meta.volume_24hr, meta.volume)
    TableWrite.update_event_metrics(
        db, event.event_id, meta.liquidity, meta.competitive
    )
    TableWrite.attach_market_to_event(
        db,
        market_id=market_id,
        event_id=event.event_id,
        outcome_label=m.label,
        icon_url=m.icon,
    )
    TableWrite.replace_market_tags(db, market_id=market_id, tags=list(m.tags))
