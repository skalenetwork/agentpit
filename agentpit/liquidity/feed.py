"""Polymarket CLOB market-channel client + event routing for the book mirror.

Connection facts (verified live, spec §3): public channel, subscribe with
{"operation": "subscribe", "assets_ids": [...]}; client sends the text frame
"PING" every 10s; PING/PONG is NOT a data-liveness signal (known silent-freeze
server bug), so an event-inactivity watchdog forces a reconnect, and the fresh
'book' snapshots delivered on re-subscribe are the resync point. Messages may
be a JSON array of events or a single event object.
"""
import asyncio
import json
import logging
import random
from collections import deque
from dataclasses import dataclass

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK

from agentpit.datastructures.user import User
from agentpit.liquidity.replica import BookReplica, BookSnapshot

log = logging.getLogger(__name__)

WSS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
FRAME_ASSETS = 200
PING_SECONDS = 10.0
SNAPSHOT_SECONDS = 30.0
RECONNECT_MIN_SECONDS = 1.0
RECONNECT_MAX_SECONDS = 60.0


@dataclass(frozen=True)
class MarketRef:
    """Everything the mirror needs per market, both id namespaces resolved."""
    market_id: int
    condition_id: str    # LOCAL condition id (hex str)
    yes_token: str       # local erc1155_tokens[0][0]
    no_token: str        # local erc1155_tokens[1][0]
    pm_yes_token: str    # POLYMARKET_YES_TOKEN_ID — subscription key
    pm_condition: str


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
    def __init__(self, refs: list[MarketRef]):
        self.by_asset: dict[str, MarketRef] = {}
        self.by_token: dict[str, tuple[MarketRef, bool]] = {}
        self.replicas: dict[str, BookReplica] = {}
        self.emptied: set[str] = set()
        self.trades: deque[dict] = deque(maxlen=10_000)
        self.set_targets(refs)

    def set_targets(self, refs: list[MarketRef]) -> None:
        new = {r.pm_yes_token: r for r in refs}
        for a in self.by_asset.keys() - new.keys():
            self.replicas.pop(a, None)
        for a in new.keys() - self.by_asset.keys():
            self.replicas[a] = BookReplica(a)
        self.by_asset = new
        self.by_token = {
            t: (r, yes)
            for r in refs
            for t, yes in ((r.yes_token, True), (r.no_token, False))
        }

    def handle_event(self, ev: dict) -> None:
        et = ev.get("event_type")
        if et == "book":
            rep = self.replicas.get(ev.get("asset_id"))
            had = rep is not None and bool(rep.bids or rep.asks)
            if rep is not None and rep.apply_book(ev):
                if had and not (rep.bids or rep.asks):
                    self.emptied.add(rep.asset_id)
        elif et == "price_change":
            for entry in ev.get("price_changes") or []:
                if not isinstance(entry, dict):
                    continue
                rep = self.replicas.get(entry.get("asset_id"))
                had = rep is not None and bool(rep.bids or rep.asks)
                if rep is not None and rep.apply_price_change_entry(entry):
                    if had and not (rep.bids or rep.asks):
                        self.emptied.add(rep.asset_id)
        elif et == "last_trade_price":
            if ev.get("asset_id") in self.by_asset:
                self.trades.append(ev)


@dataclass(frozen=True, slots=True)
class House:
    user: User
    state: MirrorState


HOUSE: House | None = None


def quote(token: str) -> tuple[MarketRef, bool, BookReplica] | None:
    house = HOUSE
    if house is None or (hit := house.state.by_token.get(token)) is None:
        return None
    rep = house.state.replicas.get(hit[0].pm_yes_token)
    return None if rep is None else (hit[0], hit[1], rep)


def book(token: str) -> BookSnapshot | None:
    q = quote(token)
    if q is None:
        return None
    snap = q[2].snapshot()
    return snap if snap is None or q[1] else snap.flipped()


def tops(tokens: list[str]) -> dict[str, tuple[int | None, int | None]]:
    out: dict[str, tuple[int | None, int | None]] = {}
    for t in tokens:
        snap = book(t)
        if snap is not None and (snap.bids or snap.asks):
            out[t] = (
                snap.bids[0][0] if snap.bids else None,
                snap.asks[0][0] if snap.asks else None,
            )
    return out


def sided() -> list[str]:
    house = HOUSE
    if house is None:
        return []
    return [
        r.yes_token
        for r in tuple(house.state.by_asset.values())
        if (snap := book(r.yes_token)) is not None and snap.bids and snap.asks
    ]


class FeedConnection:
    def __init__(self, state: MirrorState, watchdog_seconds: float):
        self.state = state
        self.assets: set[str] = set()
        self._watchdog_seconds = watchdog_seconds
        self._ws: ClientConnection | None = None
        self._awaiting: dict[str, float] = {}

    async def subscribe(self, assets: list[str]) -> None:
        self.assets.update(assets)
        self._awaiting.update(dict.fromkeys(assets, asyncio.get_running_loop().time()))
        await self._send("subscribe", assets)

    async def unsubscribe(self, assets: list[str]) -> None:
        self.assets.difference_update(assets)
        for a in assets:
            self._awaiting.pop(a, None)
        await self._send("unsubscribe", assets)

    async def _send(self, operation: str, assets: list[str]) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            for chunk in shard(assets, FRAME_ASSETS):
                await ws.send(json.dumps({"operation": operation, "assets_ids": chunk}))
        except ConnectionClosed:
            pass

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        delay = RECONNECT_MIN_SECONDS
        while True:
            try:
                async with connect(WSS_URL, max_size=None, proxy=None) as ws:
                    self._ws = ws
                    await self.subscribe(list(self.assets))
                    last_event = last_ping = loop.time()
                    while loop.time() - last_event < self._watchdog_seconds:
                        if loop.time() - last_ping >= PING_SECONDS:
                            await ws.send("PING")
                            last_ping = loop.time()
                            late = [a for a, t in self._awaiting.items()
                                    if last_ping - t >= SNAPSHOT_SECONDS]
                            if late:
                                log.info("mirror feed: %d assets have no snapshot "
                                         "%.0fs after subscribe, resubscribing",
                                         len(late), SNAPSHOT_SECONDS)
                                await self._send("unsubscribe", late)
                                await self.subscribe(late)
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=PING_SECONDS)
                        except TimeoutError:
                            continue
                        events = parse_events(raw)
                        if events:
                            last_event = loop.time()
                        for ev in events:
                            self.state.handle_event(ev)
                            if ev.get("event_type") == "book":
                                self._awaiting.pop(ev.get("asset_id", ""), None)
                                delay = RECONNECT_MIN_SECONDS
                    log.warning(
                        "mirror feed watchdog tripped (%ss silent, %d assets), reconnecting",
                        self._watchdog_seconds, len(self.assets))
            except Exception as exc:
                me = asyncio.current_task()
                if me is not None and me.cancelling():
                    # Closing the websocket is part of being cancelled, and on a
                    # dead socket the close itself raises. That error is not a
                    # reason to reconnect: treating it as one lost the cancel and
                    # left this connection writing into books beside the feed
                    # that replaced it.
                    raise asyncio.CancelledError from exc
                if isinstance(exc, ConnectionClosedOK):
                    log.info("mirror feed connection closed (%d assets): %s",
                             len(self.assets), exc)
                else:
                    log.exception("mirror feed connection error (%d assets)",
                                  len(self.assets))
            finally:
                self._ws = None
                for a in self.assets:
                    rep = self.state.replicas.get(a)
                    if rep is not None:
                        rep.mark_stale()
            await asyncio.sleep(random.uniform(0, delay))
            delay = min(delay * 2, RECONNECT_MAX_SECONDS)
