"""Pure in-memory replica of one Polymarket order book (one asset_id).

Fed by CLOB WSS market-channel events. No I/O. All prices/sizes are integer
micro units (1_000_000 == $1.00 == 1 share), parsed from the feed's decimal
STRINGS via Decimal — never through float.
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, Overflow

from agentpit.datastructures.match import Take

MICRO = 1_000_000
TICK = 1_000  # 0.001 — the local book's price grid


def to_micro(value) -> int | None:
    """Decimal string -> integer micro units; None on garbage/non-finite."""
    if value is None:
        return None
    try:
        d = Decimal(str(value))
        if not d.is_finite():  # NaN / sNaN / ±Infinity
            return None
        if d.adjusted() > 18:  # pathological magnitude (> 1e18); reject
            return None
        return int((d * MICRO).to_integral_value())
    except (InvalidOperation, ValueError, TypeError, Overflow):
        return None


@dataclass(frozen=True, slots=True)
class Used:
    seen: int
    taken: int


UseKey = tuple[str, bool, int]


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    """Immutable, validated view: levels sorted best-first."""
    asset_id: str
    bids: tuple[tuple[int, int], ...]  # (price_micro, size_micro), best (highest) first
    asks: tuple[tuple[int, int], ...]  # best (lowest) first

    def flipped(self) -> "BookSnapshot":
        return BookSnapshot(
            self.asset_id,
            tuple((MICRO - p, s) for p, s in self.asks),
            tuple((MICRO - p, s) for p, s in self.bids),
        )


def _clean_levels(levels) -> dict[int, int]:
    out: dict[int, int] = {}
    for lvl in levels or []:
        if not isinstance(lvl, dict):
            continue
        p, s = to_micro(lvl.get("price")), to_micro(lvl.get("size"))
        if p is None or s is None or s <= 0:
            continue
        if not (0 < p < MICRO) or p % TICK:
            continue  # outside (0,1) or off the local 0.001 grid
        out[p] = s
    return out


class BookReplica:
    def __init__(self, asset_id: str):
        self.asset_id = asset_id
        self.bids: dict[int, int] = {}  # price_micro -> size_micro
        self.asks: dict[int, int] = {}
        self.seeded = False
        self.used: dict[UseKey, Used] = {}

    def apply_book(self, msg: dict) -> bool:
        """Full snapshot: REPLACES the book atomically. Returns True if applied."""
        if msg.get("asset_id") != self.asset_id:
            return False
        bids = _clean_levels(msg.get("bids"))
        asks = _clean_levels(msg.get("asks"))
        self.bids, self.asks = bids, asks
        self.seeded = True
        return True

    def apply_price_change_entry(self, entry: dict) -> bool:
        """One price_changes[] entry. size is the NEW TOTAL at that level
        (replace semantics); size 0 removes the level. Returns True if applied."""
        if entry.get("asset_id") != self.asset_id or not self.seeded:
            return False
        side = entry.get("side")
        p, s = to_micro(entry.get("price")), to_micro(entry.get("size"))
        if side not in ("BUY", "SELL") or p is None or s is None or s < 0:
            return False
        if not (0 < p < MICRO) or p % TICK:
            return False
        book = self.bids if side == "BUY" else self.asks
        if s == 0:
            book.pop(p, None)
        else:
            book[p] = s
        return True

    def mark_stale(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.seeded = False

    def snapshot(self) -> BookSnapshot | None:
        """Validated frozen view, or None when unusable (unseeded/crossed)."""
        if not self.seeded:
            return None
        bids, asks = dict(self.bids), dict(self.asks)
        if bids and asks and max(bids) >= min(asks):
            return None
        return BookSnapshot(
            asset_id=self.asset_id,
            bids=tuple(sorted(bids.items(), reverse=True)),
            asks=tuple(sorted(asks.items())),
        )

    def take(self, agent: str, ask: bool, bound: int, size: int) -> tuple[Take, ...]:
        snap = self.snapshot()
        if snap is None:
            return ()
        takes: list[Take] = []
        for price, shown in snap.asks if ask else snap.bids:
            if not size or (price > bound if ask else price < bound):
                break
            u = self.used.get((agent, ask, price))
            q = min(
                shown - u.taken if u is not None and u.seen == shown else shown, size
            )
            if q > 0:
                takes.append(Take(ask, price, shown, q))
                size -= q
        return tuple(takes)

    def use(self, agent: str, takes: tuple[Take, ...]) -> None:
        for t in takes:
            u = self.used.get((agent, t.ask, t.price))
            prior = u.taken if u is not None and u.seen == t.shown else 0
            self.used[(agent, t.ask, t.price)] = Used(t.shown, prior + t.size)
        bids, asks = dict(self.bids), dict(self.asks)
        self.used = {
            k: u
            for k, u in self.used.items()
            if (asks if k[1] else bids).get(k[2]) == u.seen
        }
