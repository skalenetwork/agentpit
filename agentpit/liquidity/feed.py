"""Polymarket CLOB market-channel client + event routing for the book mirror.

Connection facts (verified live, spec §3): public channel, subscribe with
{"assets_ids": [...], "type": "market"}; ≤200 assets per connection (the real
cap ~500 fails SILENTLY — no initial snapshots); client sends the text frame
"PING" every 10s; PING/PONG is NOT a data-liveness signal (known silent-freeze
server bug), so an event-inactivity watchdog forces a reconnect, and the fresh
'book' snapshots delivered on re-subscribe are the resync point. Messages may
be a JSON array of events or a single event object.
"""
import asyncio
import json
import logging
from collections import deque
from dataclasses import dataclass

import httpx

from agentpit.liquidity.replica import BookReplica

log = logging.getLogger(__name__)

WSS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
CLOB_BOOKS_URL = "https://clob.polymarket.com/books"
# Plain non-browser clients get Cloudflare 403s on the CLOB REST API.
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; agentpit-mirror/1.0)"}


@dataclass(frozen=True)
class MarketRef:
    """Everything the mirror needs per market, both id namespaces resolved."""
    market_id: int
    condition_id: str    # LOCAL condition id (hex str) — order/cancel scoping
    yes_token: str       # local erc1155_tokens[0][0]
    no_token: str        # local erc1155_tokens[1][0]
    pm_yes_token: str    # POLYMARKET_YES_TOKEN_ID — subscription key


def parse_events(raw) -> list[dict]:
    """WSS frames arrive as a JSON array of events OR a single event object."""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    return []


def shard(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


class MirrorState:
    """Shared mutable state between the feed (writer) and reconciler (reader).
    Single event loop — no locking needed; the reconciler only reads validated
    immutable snapshots."""

    def __init__(self, refs: list[MarketRef]):
        self.by_asset: dict[str, MarketRef] = {}
        self.replicas: dict[str, BookReplica] = {}
        self.dirty: set[str] = set()       # pm asset ids needing a reconcile
        self.trades: deque = deque()       # raw last_trade_price events
        self.set_targets(refs)

    def set_targets(self, refs: list[MarketRef]) -> tuple[list[MarketRef], list[MarketRef]]:
        """Replace the target set. Returns (added, removed) refs."""
        new = {r.pm_yes_token: r for r in refs}
        added = [r for a, r in new.items() if a not in self.by_asset]
        removed = [r for a, r in self.by_asset.items() if a not in new]
        for r in removed:
            self.replicas.pop(r.pm_yes_token, None)
            self.dirty.discard(r.pm_yes_token)
        for r in added:
            self.replicas[r.pm_yes_token] = BookReplica(r.pm_yes_token)
        self.by_asset = new
        return added, removed

    def handle_event(self, ev: dict) -> None:
        et = ev.get("event_type")
        if et == "book":
            rep = self.replicas.get(ev.get("asset_id"))
            if rep is not None and rep.apply_book(ev):
                self.dirty.add(rep.asset_id)
        elif et == "price_change":
            for entry in ev.get("price_changes") or []:
                if not isinstance(entry, dict):
                    continue
                rep = self.replicas.get(entry.get("asset_id"))
                if rep is not None and rep.apply_price_change_entry(entry):
                    self.dirty.add(rep.asset_id)
        elif et == "tick_size_change":
            rep = self.replicas.get(ev.get("asset_id"))
            if rep is not None:
                rep.mark_stale()           # epoch reset — await a fresh snapshot
                self.dirty.discard(rep.asset_id)
        elif et == "last_trade_price":
            if ev.get("asset_id") in self.by_asset:
                self.trades.append(ev)


def fetch_books_rest(
    asset_ids: list[str], *, client=None, batch_size: int = 100
) -> list[dict]:
    """Batch REST seed via POST /books (rate limit 500 req/10s — fine).
    Returns raw book payloads (same shape as the WSS 'book' event)."""
    own = client is None
    cl = client if client is not None else httpx.Client(headers=_HEADERS, timeout=15.0)
    out: list[dict] = []
    try:
        for batch in shard(asset_ids, batch_size):
            try:
                resp = cl.post(CLOB_BOOKS_URL, json=[{"token_id": a} for a in batch])
                resp.raise_for_status()
                body = resp.json()
            except Exception:
                log.exception("REST /books seed failed for a batch of %d", len(batch))
                continue
            out.extend(b for b in body if isinstance(b, dict))
    finally:
        if own:
            cl.close()
    return out


async def run_connection(
    state: MirrorState,
    asset_ids: list[str],
    *,
    connect=None,
    ping_interval: float = 10.0,
    watchdog_seconds: float = 120.0,
    reconnect_delay: float = 2.0,
) -> None:
    """One sharded connection: subscribe, route events, PING on idle, and
    force a reconnect when no events arrive within the watchdog window
    (re-subscribing yields fresh 'book' snapshots — the resync point)."""
    if connect is None:
        import websockets
        connect = lambda url: websockets.connect(url)  # noqa: E731
    while True:
        try:
            async with connect(WSS_URL) as ws:
                await ws.send(json.dumps({"assets_ids": asset_ids, "type": "market"}))
                loop = asyncio.get_running_loop()
                last_event = loop.time()
                last_ping = loop.time()
                while loop.time() - last_event < watchdog_seconds:
                    if loop.time() - last_ping >= ping_interval:
                        await ws.send("PING")
                        last_ping = loop.time()
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=ping_interval)
                    except TimeoutError:
                        continue
                    events = parse_events(raw)
                    if events:
                        last_event = loop.time()
                        for ev in events:
                            state.handle_event(ev)
                log.warning(
                    "mirror feed watchdog tripped (%ss silent, %d assets) — reconnecting",
                    watchdog_seconds, len(asset_ids))
                for a in asset_ids:        # stale until the re-subscribe snapshot
                    rep = state.replicas.get(a)
                    if rep is not None:
                        rep.mark_stale()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            me = asyncio.current_task()
            if me is not None and me.cancelling():
                # Closing the websocket is part of being cancelled, and on a
                # dead socket the close itself raises. That error is not a
                # reason to reconnect: treating it as one lost the cancel and
                # left this connection writing into books beside the feed
                # that replaced it.
                raise asyncio.CancelledError from exc
            log.exception("mirror feed connection error (%d assets)", len(asset_ids))
        await asyncio.sleep(reconnect_delay)
