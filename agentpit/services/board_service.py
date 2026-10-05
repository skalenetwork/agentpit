"""The public markets board: every tab and the Sports section, rebuilt from the database and the latest valuations at most every 30 s per process."""
import math
import threading
import time
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from agentpit.config import Settings
from agentpit.datastructures.board import (
    BoardTab,
    CardKind,
    GameStatus,
    Sport,
    SportItem,
    Sports,
    WireBet,
    WireBoard,
    WireCard,
    WireGame,
    WireOutcome,
)
from agentpit.datastructures.event import Event
from agentpit.datastructures.market import Market
from agentpit.datastructures.market_state import MarketState
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.domain.sports import SPORTS, League, league_of
from agentpit.polymarket.pricing import MarketPrices, compute_market_prices
from agentpit.services.agent_profile import _money
from agentpit.services.leaderboard_service import LeaderboardService, all_holdings

BOARD_PAGE = 48
TTL = 30
WEEK = 7 * 86_400
MOVE = 0.01
VIEWS = {"trending": "Trending", "movers": "Movers", "agents": "Agents", "ending": "Ending soon", "settled": "Settled"}
SPORT_VIEWS = {"upcoming": "Upcoming", "agents": "Agents", "futures": "Futures", "settled": "Settled"}

Bets = dict[str, list[tuple[int, WireBet]]]


@dataclass(frozen=True)
class _Item[C: WireCard]:
    card: C
    text: str
    held: bool


@dataclass
class _Scope:
    games: list[_Item[WireGame]] = field(default_factory=list)
    futures: list[_Item[WireCard]] = field(default_factory=list)
    settled: int = 0
    no_book: Counter[str] = field(default_factory=Counter)

    @property
    def count(self) -> int:
        return len(self.games) + len(self.futures)


@dataclass(frozen=True)
class _Snapshot:
    built_at: float
    as_of: int
    live_markets: int
    tabs: list[BoardTab]
    lists: dict[str, list[_Item[WireCard]]]
    views: list[SportItem]
    sports: list[Sport]
    scopes: dict[str, _Scope]
    games: list[_Item[WireGame]]


@dataclass(frozen=True)
class _Entry:
    event: Event
    kind: CardKind
    rows: list[WireOutcome]
    sided: list[bool]
    active: list[bool]
    closed_out: bool
    resolved_at: int | None
    text: str
    game: bool

    @property
    def bets(self) -> list[WireBet]:
        return [b for r in self.rows for b in r.bets]

    @property
    def live(self) -> bool:
        return not self.closed_out and (any(self.sided) or any(a and r.bets for r, a in zip(self.rows, self.active)))

    def settled(self, since: int) -> bool:
        return self.closed_out and self.resolved_at is not None and self.resolved_at >= since and bool(self.bets)

    @property
    def lead(self) -> int:
        return max(range(len(self.rows)), key=lambda i: _rank(self.rows[i].price))

    @property
    def move(self) -> tuple[float, int] | None:
        moves = [
            (abs(r.change24h), i)
            for i, (r, s) in enumerate(zip(self.rows, self.sided))
            if s and r.change24h is not None and abs(r.change24h) >= MOVE
        ]
        return max(moves, key=lambda m: m[0]) if moves else None

    @property
    def closes(self) -> int:
        return (self.event.start_time if self.game else self.event.end_date) or 0


_snapshot: _Snapshot | None = None
_lock = threading.Lock()


def _rank(price: float | None) -> float:
    return -1.0 if price is None else price


def _kind(markets: list[Market]) -> CardKind:
    labels = [label for _, label in markets[0].erc1155_tokens]
    if len(markets) > 1:
        return "multi"
    return "binary" if labels == ["Yes", "No"] else "window" if labels == ["Up", "Down"] else "matchup"


def _price(m: Market, i: int, prices: dict[int, MarketPrices]) -> float | None:
    if m.market_state == MarketState.RESOLVED:
        return 1.0 if m.resolved_outcome == i else 0.0
    if m.market_state == MarketState.ACTIVE:
        return prices[m.market_id].outcome_prices[i] / 1_000_000
    return None


def _order(rows: list[tuple[WireOutcome, Market]], kind: CardKind, title: str) -> list[tuple[WireOutcome, Market]]:
    if kind != "multi":
        return rows
    draws = [r for r in rows if r[0].label.startswith("Draw")]
    if len(rows) == 3 and len(draws) == 1:
        a, b = sorted((r for r in rows if r is not draws[0]), key=lambda r: (r[0].label not in title, title.find(r[0].label)))
        return [a, draws[0], b]
    return sorted(rows, key=lambda r: -_rank(r[0].price))


def _entry(
    event: Event,
    markets: list[Market],
    tags: set[str],
    prices: dict[int, MarketPrices],
    tops: dict[str, tuple[int | None, int | None]],
    open_bets: Bets,
    settled_bets: Bets,
) -> _Entry:
    kind = _kind(markets)
    closed_out = not any(m.market_state == MarketState.ACTIVE for m in markets)
    bets = settled_bets if closed_out else open_bets
    shown = markets if closed_out else [m for m in markets if m.market_state != MarketState.RESOLVED]
    per_token = kind in ("matchup", "window")
    rows: list[tuple[WireOutcome, Market]] = []
    for m in shown:
        change = None if closed_out else m.price_change_24h
        held = bets.get(m.condition_id.value, [])
        rows.extend(
            (
                WireOutcome(
                    label=label if per_token else m.outcome_label or m.question,
                    question=m.question,
                    slug=m.slug,
                    url=m.url,
                    price=_price(m, i, prices),
                    change24h=None if change is None else change if i == 0 else -change,
                    bets=[b for k, b in held if not per_token or k == i],
                ),
                m,
            )
            for i, (_, label) in enumerate(m.erc1155_tokens if per_token else m.erc1155_tokens[:1])
        )
    rows = _order(rows, kind, event.title)
    resolved = [m.resolved_at or m.end_date for m in markets if m.market_state == MarketState.RESOLVED]
    return _Entry(
        event=event,
        kind=kind,
        rows=[r for r, _ in rows],
        sided=[m.market_state == MarketState.ACTIVE and None not in tops.get(m.erc1155_tokens[0][0], (None, None)) for _, m in rows],
        active=[m.market_state == MarketState.ACTIVE for _, m in rows],
        closed_out=closed_out,
        resolved_at=max((t for t in resolved if t is not None), default=None),
        text=" ".join([event.title, *(m.question for m in markets), *(m.outcome_label or "" for m in markets)]).casefold(),
        game="games" in tags,
    )


def _card[C: WireCard](entry: _Entry, lead: int, icon: str | None, cls: type[C], **extra: object) -> _Item[C]:
    rows = entry.rows
    keep = list(range(len(rows))) if len(rows) <= 3 else [i for i, r in enumerate(rows) if i == lead or r.bets]
    event = entry.event
    card = cls(
        slug=event.slug,
        title=event.title,
        icon=icon,
        category=event.category,
        url=event.url,
        kind=entry.kind,
        state="settled" if entry.closed_out else "live",
        endDate=event.end_date,
        resolvedAt=entry.resolved_at if entry.closed_out else None,
        outcomeCount=len(rows),
        lead=keep.index(lead),
        outcomes=[rows[i] for i in keep],
        **extra,
    )
    return _Item(card=card, text=entry.text, held=bool(entry.bets))


def _bets(names: dict[str, str]) -> tuple[Bets, Bets]:
    open_bets: Bets = {}
    settled_bets: Bets = {}
    for address, held in all_holdings().items():
        name = names.get(address)
        if name is None:
            continue
        placed = [(settled_bets if p.settled else open_bets, p) for p in held.positions]
        placed += [(settled_bets, p) for p in held.closed if p.curPrice in (0.0, 1.0) and p.realizedPnl == 0]
        for target, p in placed:
            target.setdefault(p.conditionId, []).append(
                (
                    p.outcomeIndex,
                    WireBet(agent=address, name=name, side=p.outcome, value=_money(p.currentValue), avgPrice=p.avgPrice, pnl=_money(p.cashPnl)),
                )
            )
    return open_bets, settled_bets


def _money_of(item: _Item[WireCard]) -> int:
    return sum(int(b.value) for o in item.card.outcomes for b in o.bets)


def _agents_of(item: _Item[WireCard]) -> int:
    return len({b.agent for o in item.card.outcomes for b in o.bets})


def _when(item: _Item[WireGame]) -> int:
    return item.card.startTime or item.card.endDate or 0


def _matching[C: WireCard](items: Iterable[_Item[C]], needle: str | None) -> list[_Item[C]]:
    return [i for i in items if needle is None or needle in i.text]


def _sports(
    entries: list[tuple[_Entry, _Item[WireCard], _Item[WireGame] | None, League]], since: int
) -> tuple[list[SportItem], list[Sport], dict[str, _Scope], list[_Item[WireGame]]]:
    by_league: dict[str, _Scope] = {}
    leagues: dict[str, League] = {}
    views = {key: _Scope() for key in SPORT_VIEWS}
    games: list[_Item[WireGame]] = []
    for entry, future, game, league in entries:
        scope = by_league.setdefault(league.key, _Scope())
        leagues[league.key] = league
        if game is None:
            if entry.live:
                scope.futures.append(future)
                views["futures"].futures.append(future)
                if future.held:
                    views["agents"].futures.append(future)
            continue
        status = game.card.status
        if status != "settled":
            games.append(game)
        if status == "upcoming" or (status == "started" and game.held):
            scope.games.append(game)
            if status == "upcoming":
                views["upcoming"].games.append(game)
            if game.held:
                views["agents"].games.append(game)
        elif status == "started":
            scope.no_book[league.key] += 1
            views["upcoming"].no_book[league.key] += 1
        elif entry.settled(since):
            scope.settled += 1
            views["settled"].games.append(game)
    for scope in (*by_league.values(), *views.values()):
        scope.games.sort(key=_when)
    views["settled"].games.sort(key=lambda g: -(g.card.resolvedAt or 0))
    scopes: dict[str, _Scope] = dict(views)
    sports: list[Sport] = []
    for sport, label in SPORTS.items():
        own = sorted(
            ((k, s) for k, s in by_league.items() if leagues[k].sport == sport and (s.count or s.settled)),
            key=lambda ks: -ks[1].count,
        )
        if not own:
            continue
        whole = _Scope(
            games=sorted((g for _, s in own for g in s.games), key=_when),
            futures=[f for _, s in own for f in s.futures],
            settled=sum(s.settled for _, s in own),
            no_book=sum((s.no_book for _, s in own), Counter()),
        )
        scopes[sport] = whole
        listed = own if len(own) > 1 else []
        scopes.update({f"{sport}/{k}": s for k, s in listed})
        sports.append(
            Sport(
                key=sport,
                label=label,
                count=whole.count,
                leagues=[SportItem(key=f"{sport}/{k}", label=leagues[k].label, count=s.count) for k, s in listed],
            )
        )
    sports.sort(key=lambda s: -s.count)
    items = [SportItem(key=k, label=label, count=views[k].count) for k, label in SPORT_VIEWS.items()]
    games.sort(key=_when)
    return items, sports, scopes, games


class BoardService:
    def __init__(self, db: DbSession, leaderboard: LeaderboardService, settings: Settings) -> None:
        self._db = db
        self._leaderboard = leaderboard
        self._settings = settings

    def board(self, tab: str, q: str | None, page: int, sport: str) -> WireBoard | None:
        snap = self._snapshot()
        needle = q.casefold() if q else None
        sports = None
        cards: list[WireCard] = []
        if tab == "sports":
            scope = snap.scopes.get(sport)
            if scope is None:
                return None
            games, futures = scope.games, scope.futures
            if needle is not None:
                games, futures = _matching(snap.games, needle), _matching(snap.scopes["futures"].futures, needle)
            total, pages, page = len(games) + len(futures), 1, 1
            sports = Sports(
                item=sport,
                views=snap.views,
                sports=snap.sports,
                games=[i.card for i in games],
                futures=[i.card for i in futures],
                settled=scope.settled,
                noBook=dict(scope.no_book),
            )
        else:
            listed = snap.lists.get(tab)
            if listed is None:
                return None
            rows = _matching(listed, needle)
            total = len(rows)
            pages = max(1, math.ceil(total / BOARD_PAGE))
            page = min(page, pages)
            cards = [i.card for i in rows[(page - 1) * BOARD_PAGE : page * BOARD_PAGE]]
        return WireBoard(
            asOf=snap.as_of,
            liveMarkets=snap.live_markets,
            tab=tab,
            tabs=snap.tabs,
            q=q,
            page=page,
            pages=pages,
            total=total,
            cards=cards,
            sports=sports,
        )

    def _snapshot(self) -> _Snapshot:
        global _snapshot
        with _lock:
            if _snapshot is None or time.monotonic() - _snapshot.built_at >= TTL:
                _snapshot = self._build()
            return _snapshot

    def _build(self) -> _Snapshot:
        now = int(time.time())
        since = now - WEEK
        excluded = {"excluded_categories": self._settings.excluded_categories, "excluded_tags": self._settings.excluded_tags}
        with self._db.read() as conn:
            pairs = sorted(
                TableRead.board_events(conn, settled_since=since, **excluded),
                key=lambda em: (em[0].volume_24hr is None, -(em[0].volume_24hr or 0), -em[0].event_id),
            )
            active = [m for _, ms in pairs for m in ms if m.market_state == MarketState.ACTIVE]
            tokens = [t for m in active for t, _ in m.erc1155_tokens]
            tops = TableRead.book_tops_for_tokens(conn, tokens)
            lasts = TableRead.last_trade_prices_for_tokens(conn, [t for t in tokens if t not in tops])
            tags = TableRead.tag_slugs_by_event(conn, [e.event_id for e, _ in pairs])
            live_markets = TableRead.count_active_markets(conn, **excluded)
        prices = {m.market_id: compute_market_prices(m, tops, lasts) for m in active}
        open_bets, settled_bets = _bets({r.address: r.name for r in self._leaderboard.build_board()})

        live: list[tuple[_Entry, _Item[WireCard]]] = []
        settled: list[tuple[_Entry, _Item[WireCard]]] = []
        movers: list[tuple[float, _Item[WireCard]]] = []
        sports: list[tuple[_Entry, _Item[WireCard], _Item[WireGame] | None, League]] = []
        for event, ms in pairs:
            own = tags.get(event.event_id, set())
            entry = _entry(event, ms, own, prices, tops, open_bets, settled_bets)
            icon = event.icon_url or next((m.icon_url for m in ms if m.icon_url), None)
            item = _card(entry, entry.lead, icon, WireCard)
            if entry.live:
                live.append((entry, item))
                move = entry.move
                if move is not None:
                    movers.append((move[0], _card(entry, move[1], icon, WireCard)))
            elif entry.settled(since):
                settled.append((entry, item))
            if event.category == "Sports":
                league = league_of(event.series_slug, own)
                game = None
                if entry.game:
                    status: GameStatus = "settled" if entry.closed_out else "upcoming" if any(entry.sided) else "started"
                    game = _card(
                        entry,
                        entry.lead,
                        icon,
                        WireGame,
                        league=league.key,
                        leagueLabel=league.label,
                        sport=league.sport,
                        status=status,
                        startTime=event.start_time,
                    )
                sports.append((entry, item, game, league))

        trending = [i for _, i in live]
        lists = {
            "trending": trending,
            "movers": [i for _, i in sorted(movers, key=lambda mi: -mi[0])],
            "agents": sorted((i for i in trending if i.held), key=lambda i: (-_agents_of(i), -_money_of(i))),
            "ending": [i for e, i in sorted(live, key=lambda ei: ei[0].closes) if now < e.closes <= now + WEEK],
            "settled": [i for _, i in sorted(settled, key=lambda ei: -(ei[0].resolved_at or 0))],
        }
        tabs = [BoardTab(key=k, label=label, count=len(lists[k]), category=False) for k, label in VIEWS.items()]
        for category, count in Counter(e.event.category for e, _ in live if e.event.category).most_common():
            if count < 10:
                break
            key = category.lower().replace(" ", "-")
            lists[key] = [i for i in trending if i.card.category == category]
            tabs.append(BoardTab(key=key, label=category, count=count, category=True))
        views, sport_list, scopes, games = _sports(sports, since)
        return _Snapshot(
            built_at=time.monotonic(),
            as_of=now,
            live_markets=live_markets,
            tabs=tabs,
            lists=lists,
            views=views,
            sports=sport_list,
            scopes=scopes,
            games=games,
        )
