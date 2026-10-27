from agentpit.datastructures.event_sort import EventSort
from agentpit.datastructures.event_with_markets import (
    ListEventCategoriesResponse,
    EventWithMarkets,
    ListEventsResponse,
)
from agentpit.datastructures.gamma_market import GammaEvent
from agentpit.datastructures.tag import ListTagsResponse, TagFacet, TagNavEntry
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import EventNotFoundError, InvalidPaginationError
from agentpit.polymarket.gamma import to_gamma_event
from agentpit.polymarket.pricing import prices_for_markets
from agentpit.polymarket.tag_taxonomy import (
    BLOCKED_SLUGS,
    DEPRECATED_PREFIX,
    MAX_FACET_COVERAGE,
    MAX_FACETS,
    MIN_NAV_EVENTS,
    NAV_SLUGS,
)


class EventService:
    def __init__(
        self,
        db: DbSession,
        excluded_categories: "list[str] | None" = None,
        excluded_tags: "list[str] | None" = None,
    ):
        self._db = db
        # Defaults to "exclude nothing" so the internal constructions of this
        # service (orphan wrapping, singleton creation) keep their old
        # behaviour: those write rows, and a write path has no business
        # consulting a browse filter.
        self._excluded_categories = excluded_categories or []
        self._excluded_tags = excluded_tags or []

    def list_events(
        self,
        limit: int,
        offset: int,
        category: str | None = None,
        tag: str | None = None,
        subtags: list[str] | None = None,
        sort: EventSort | None = None,
    ) -> ListEventsResponse:
        if limit < 1 or limit > 1000:
            raise InvalidPaginationError("limit must be between 1 and 1000")
        if offset < 0:
            raise InvalidPaginationError("offset must be non-negative")
        with self._db.read() as conn:
            pairs, total = TableRead.list_events_with_markets(
                conn,
                limit=limit,
                offset=offset,
                category=category,
                tag=tag,
                subtags=subtags,
                sort=sort,
                excluded_categories=self._excluded_categories,
                excluded_tags=self._excluded_tags,
            )
        events = [
            EventWithMarkets(event=event, markets=markets) for event, markets in pairs
        ]
        return ListEventsResponse(
            events=events, total=total, limit=limit, offset=offset
        )

    def list_events_gamma(
        self,
        limit: int,
        offset: int,
        category: str | None = None,
        tag: str | None = None,
        subtags: list[str] | None = None,
        sort: EventSort | None = None,
        closed: bool | None = None,
    ) -> list[GammaEvent]:
        if limit < 1 or limit > 1000:
            raise InvalidPaginationError("limit must be between 1 and 1000")
        if offset < 0:
            raise InvalidPaginationError("offset must be non-negative")
        with self._db.read() as conn:
            pairs, _total = TableRead.list_events_with_markets(
                conn,
                limit=limit,
                offset=offset,
                category=category,
                tag=tag,
                subtags=subtags,
                sort=sort,
                closed=closed,
                excluded_categories=self._excluded_categories,
                excluded_tags=self._excluded_tags,
            )
            all_markets = [m for _event, markets in pairs for m in markets]
            prices = prices_for_markets(conn, all_markets)
        return [to_gamma_event(event, markets, prices) for event, markets in pairs]

    def get_event_gamma(self, slug: str) -> GammaEvent:
        with self._db.read() as conn:
            event = TableRead.get_event_by_slug(conn, slug)
            if event is None:
                raise EventNotFoundError(slug)
            markets = TableRead.list_markets_by_event_id(conn, event.event_id)
            prices = prices_for_markets(conn, markets)
        return to_gamma_event(event, markets, prices)

    def list_categories(self) -> ListEventCategoriesResponse:
        with self._db.read() as conn:
            categories = TableRead.list_event_categories(
                conn,
                excluded_categories=self._excluded_categories,
                excluded_tags=self._excluded_tags,
            )
        return ListEventCategoriesResponse(categories=categories)

    def list_tags(self) -> ListTagsResponse:
        """The curated top-level list, each entry carrying its own facets.

        Facets are nested rather than served from a `?parent=` endpoint because
        the sidebar renders a chevron for every category at once and must know
        which ones have subcategories before any is expanded.

        One nav query plus one facet query per surviving slug — about
        seventeen indexed reads, behind a TTL cache, against data that only
        changes when an hourly sync runs. A single self-joining query would do
        it in one round trip and be markedly harder to read.
        """
        with self._db.read() as conn:
            present = {
                slug: (label, count)
                for slug, label, count in TableRead.list_tag_nav(
                    conn,
                    slugs=list(NAV_SLUGS),
                    min_events=MIN_NAV_EVENTS,
                    excluded_categories=self._excluded_categories,
                    excluded_tags=self._excluded_tags,
                )
            }
            entries: list[TagNavEntry] = []
            # Iterating NAV_SLUGS, not `present`, is what restores the curated
            # order the query's GROUP BY does not preserve.
            for slug in NAV_SLUGS:
                found = present.get(slug)
                if found is None:
                    continue
                label, count = found
                facets = TableRead.list_tag_facets(
                    conn,
                    parent_slug=slug,
                    blocked=BLOCKED_SLUGS,
                    deprecated_prefix=DEPRECATED_PREFIX,
                    limit=MAX_FACETS,
                    max_coverage=MAX_FACET_COVERAGE,
                )
                entries.append(
                    TagNavEntry(
                        slug=slug,
                        label=label,
                        count=count,
                        facets=[
                            TagFacet(slug=s, label=lbl, count=c) for s, lbl, c in facets
                        ],
                    )
                )
        return ListTagsResponse(tags=entries)

    def get_event_by_slug(self, slug: str) -> EventWithMarkets:
        with self._db.read() as conn:
            event = TableRead.get_event_by_slug(conn, slug)
            if event is None:
                raise EventNotFoundError(slug)
            markets = TableRead.list_markets_by_event_id(conn, event.event_id)
        return EventWithMarkets(event=event, markets=markets)

    def ensure_singleton_events_for_orphans(self) -> int:
        """Wrap every market with no event in a singleton event.

        Idempotent: orphans become bound after the first pass, so subsequent
        passes return 0. Uses the market's own slug as the event slug and the
        market's question as the event title to give the home-page card a
        stable identity.
        """
        wrapped = 0
        with self._db.write() as conn:
            orphans = TableRead.list_orphan_markets(conn)
            for market in orphans:
                _bind_singleton(conn, market)
                wrapped += 1
        return wrapped

    def wrap_market_in_singleton_event_if_needed(
        self,
        market_id: int,
        category: str | None = None,
    ) -> None:
        """Enforce the "every market belongs to an event" invariant for a
        single market — e.g. after `POST /markets` creates a local market
        with no event_id. No-op if the market is already bound.
        """
        with self._db.write() as conn:
            market = TableRead.read_market(conn, market_id)
            if market is None or market.event_id is not None:
                return
            _bind_singleton(conn, market, category=category)


def _bind_singleton(conn, market, category: str | None = None) -> None:
    event = TableWrite.upsert_event(
        conn,
        slug=market.slug,
        title=market.question,
        description=market.description,
        category=category,
        start_date=market.start_date,
        end_date=market.end_date,
    )
    TableWrite.attach_market_to_event(
        conn, market_id=market.market_id, event_id=event.event_id
    )
