"""Diff a Polymarket BookSnapshot against the mirror account's live orders.

Pure functions first; the DB/chain applier (reconcile_market) follows them.
Invariant (spec §5): desired levels derived from a non-crossed snapshot are
non-crossed per token AND across the YES/NO complement map, so the mirror can
never self-match — provided cancels are applied before placements.
"""
import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal

from agentpit.config import Settings
from agentpit.datastructures.place_order_request import PlaceOrderRequest
from agentpit.datastructures.user import User
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.domain.exceptions import AdminGasPausedError
from agentpit.liquidity.replica import MICRO, BookSnapshot
from agentpit.onchain.admin import OnchainAdmin
from agentpit.services.order_service import OrderService

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LiveLevel:
    order_id: str
    token_id: str
    side: str          # "BUY" | "SELL"
    price_micro: int
    size_micro: int    # REMAINING_AMOUNT


@dataclass(frozen=True)
class Placement:
    token_id: str
    side: str
    price_micro: int
    size_micro: int


def desired_levels(
    snap: BookSnapshot, yes_token: str, no_token: str, depth: int = 0
) -> list[Placement]:
    """YES book verbatim + NO book as the exact 1-p complement (same sizes).

    `depth` caps how many levels per side (nearest the touch) are mirrored;
    snap.bids/asks are already best-first, so a slice takes the top of book.
    `depth <= 0` mirrors the full book (1:1)."""
    bids = snap.bids[:depth] if depth > 0 else snap.bids
    asks = snap.asks[:depth] if depth > 0 else snap.asks
    out: list[Placement] = []
    for p, s in bids:
        out.append(Placement(yes_token, "BUY", p, s))
        out.append(Placement(no_token, "SELL", MICRO - p, s))
    for p, s in asks:
        out.append(Placement(yes_token, "SELL", p, s))
        out.append(Placement(no_token, "BUY", MICRO - p, s))
    return out


@dataclass(frozen=True)
class HotCuts:
    """Price boundary of the hot band, per side of the YES book.

    `bid_cut` is the price of the hot-depth-th bid, `ask_cut` that of the
    hot-depth-th ask. `None` means the side has fewer levels than the hot
    depth (or the hot depth is unbounded), so the whole side is hot.
    """
    bid_cut: int | None
    ask_cut: int | None


def hot_cuts(snap: BookSnapshot, hot_depth: int) -> HotCuts:
    """Derive the hot band from a snapshot. `hot_depth <= 0` = everything hot."""
    if hot_depth <= 0:
        return HotCuts(None, None)
    bid_cut = snap.bids[hot_depth - 1][0] if len(snap.bids) >= hot_depth else None
    ask_cut = snap.asks[hot_depth - 1][0] if len(snap.asks) >= hot_depth else None
    return HotCuts(bid_cut, ask_cut)


def is_hot_level(
    token_id: str, side: str, price_micro: int, cuts: HotCuts, yes_token: str
) -> bool:
    """Is this (token, side, price) inside the hot band?

    The YES book is mirrored verbatim and the NO book as the MICRO-p
    complement, so each of the four placement shapes tests a different
    inequality. Derived from the CURRENT snapshot, so a level migrates
    between tiers on its own as the touch moves.
    """
    is_yes = token_id == yes_token
    if (is_yes and side == "BUY") or (not is_yes and side == "SELL"):
        # Bid side: YES BUY @p, or its complement NO SELL @MICRO-p.
        if cuts.bid_cut is None:
            return True
        p = price_micro if is_yes else MICRO - price_micro
        return p >= cuts.bid_cut
    # Ask side: YES SELL @p, or its complement NO BUY @MICRO-p.
    if cuts.ask_cut is None:
        return True
    p = price_micro if is_yes else MICRO - price_micro
    return p <= cuts.ask_cut


def tier_plan(
    snap: BookSnapshot,
    yes_token: str,
    no_token: str,
    *,
    hot_depth: int,
    max_depth: int,
    cold: bool,
) -> tuple[list[Placement], "Callable[[LiveLevel], bool] | None"]:
    """What one reconcile pass should target, and what it must leave alone.

    Cold pass: the full cap, protecting nothing — it also prunes deep levels
    that vanished upstream. Hot pass: the hot band only, protecting every live
    order outside it. When the hot depth already covers the cap there is no
    cold band, so the hot pass IS the full reconcile and protects nothing —
    that is the shipped default and it is identical to the legacy behaviour.
    """
    if cold or hot_depth <= 0 or (0 < max_depth <= hot_depth):
        return desired_levels(snap, yes_token, no_token, max_depth), None
    cuts = hot_cuts(snap, hot_depth)
    desired = desired_levels(snap, yes_token, no_token, hot_depth)
    return desired, lambda o: not is_hot_level(
        o.token_id, o.side, o.price_micro, cuts, yes_token
    )


def diff_levels(
    desired: list[Placement],
    current: list[LiveLevel],
    protect: "Callable[[LiveLevel], bool] | None" = None,
) -> tuple[list[str], list[Placement]]:
    """(order_ids to cancel, placements to make). Orders are immutable, so a
    size change at a level is cancel + re-place. One live order per
    (token, side, price) is kept; duplicates are cancelled.

    `protect` marks live orders that are OUT OF SCOPE for this pass: they are
    neither cancelled nor counted as satisfying a desired level. A hot pass
    protects the cold band so it does not wipe the deep book on every update.
    """
    want = {(d.token_id, d.side, d.price_micro): d.size_micro for d in desired}
    keep: set[tuple[str, str, int]] = set()
    cancels: list[str] = []
    for o in current:
        if protect is not None and protect(o):
            continue
        key = (o.token_id, o.side, o.price_micro)
        if key in want and want[key] == o.size_micro and key not in keep:
            keep.add(key)
        else:
            cancels.append(o.order_id)
    places = [
        Placement(token, side, price, size)
        for (token, side, price), size in want.items()
        if (token, side, price) not in keep
    ]
    return cancels, places


def split_target_micro(snap: BookSnapshot) -> int:
    """CTF inventory needed to back every SELL: YES asks need YES tokens, NO
    asks mirror the YES bid side. A split mints YES+NO equally, so the target
    is the max of the two ask-side sums (spec §8)."""
    yes_ask_sum = sum(s for _, s in snap.asks)
    no_ask_sum = sum(s for _, s in snap.bids)
    return max(yes_ask_sum, no_ask_sum)


def cap_sells_to_inventory(
    places: list[Placement], inventory_micro: dict[str, int]
) -> list[Placement]:
    """Fill SELL placements lowest-price-first, skipping any that exceed
    remaining per-token inventory (greedy best-effort — a too-big best ask is
    dropped, smaller worse-priced asks may still fit). BUYs pass through (USDC
    is never the binding constraint). Result preserves no particular order."""
    remaining = dict(inventory_micro)
    out = [p for p in places if p.side == "BUY"]
    sells = sorted((p for p in places if p.side == "SELL"), key=lambda p: p.price_micro)
    for p in sells:
        held = remaining.get(p.token_id, 0)
        if p.size_micro <= held:
            remaining[p.token_id] = held - p.size_micro
            out.append(p)
    return out


def inventory_mint_micro(need_micro: int, held_micro: int, seed_micro: int) -> int:
    """How much to split-mint, given the requirement, the stock and the block size.

    Zero when the stock already covers the requirement. Otherwise enough to leave
    the stock exactly `seed_micro` ABOVE the requirement.

    The requirement is a running maximum of a fluctuating quantity — the house
    never spends this inventory, so `held` only grows — and a running maximum
    keeps setting records forever. Topping up to the exact requirement therefore
    buys one transaction per record, indefinitely: production ran 64 splits/min
    across ~1500 markets, some growing the stock by under half a percent. Minting
    a whole block past the requirement instead converges a market in one split
    and keeps it quiet until it genuinely outgrows the block.

    `seed_micro=0` reproduces exact top-ups.
    """
    if need_micro <= 0 or held_micro >= need_micro:
        return 0
    return need_micro + seed_micro - held_micro


def _read_balances(
    onchain: OnchainAdmin, user: User, ref
) -> "tuple[dict[str, int], int]":
    """The three balances a pass needs, fetched in one round trip.

    They are independent of each other and each is a remote call. Measured
    against the SKALE node on 2026-08-12, in the api container: three calls
    in a row take 1583ms, the same three together take 545ms. Against a local
    anvil both are noise, which is why the sequential version survived this
    long.

    Threads rather than async: `OnchainAdmin` is a synchronous web3 client and
    the wait is socket I/O, which releases the GIL. Sharing one client across
    them was verified rather than assumed — five runs of three concurrent
    reads returned numbers identical to the sequential ones.
    """
    with ThreadPoolExecutor(max_workers=3) as pool:
        yes = pool.submit(onchain.ctf_balance, user.eth_address, int(ref.yes_token))
        no = pool.submit(onchain.ctf_balance, user.eth_address, int(ref.no_token))
        usd = pool.submit(onchain.usd_balance, user.eth_address)
        return {ref.yes_token: yes.result(), ref.no_token: no.result()}, usd.result()


def _ensure_inventory(
    onchain: OnchainAdmin,
    user: User,
    ref,
    snap: BookSnapshot,
    cfg: Settings,
    held: "dict[str, int]",
) -> int:
    """Split-mint CTF inventory to back every SELL, a block at a time.

    Takes the holdings the caller already read rather than reading them again:
    they were two of the five remote round trips a pass used to spend, and the
    caller needs them regardless. Returns the number of split txs performed
    (0 or 1 per call — the split is a house-key transaction, budgeted by the
    caller); a non-zero return means `held` is now stale.
    """
    need = int(Decimal(str(cfg.mirror_inventory_buffer)) * split_target_micro(snap))
    if need <= 0:
        return 0
    add = inventory_mint_micro(
        need,
        min(held[ref.yes_token], held[ref.no_token]),
        cfg.mirror_inventory_seed_micro,
    )
    if add <= 0:
        return 0
    condition_bytes = bytes.fromhex(ref.condition_id[2:])
    onchain.user_split_position(user.eth_key, condition_bytes, add)
    return 1


def _crosses(p: Placement, foreign_bid: int | None, foreign_ask: int | None) -> bool:
    if p.side == "BUY":
        return foreign_ask is not None and p.price_micro >= foreign_ask
    return foreign_bid is not None and p.price_micro <= foreign_bid


def _merge_touch(own: tuple[int | None, int | None],
                 comp: tuple[int | None, int | None]) -> tuple[int | None, int | None]:
    """Effective foreign touch on one token: its own orders PLUS the
    complement token's same-side orders mapped through 1-p (the matcher's
    MINT/MERGE paths treat those as crossable at the same inclusive
    boundary)."""
    own_bid, own_ask = own
    comp_bid, comp_ask = comp
    via_bid = MICRO - comp_ask if comp_ask is not None else None   # comp SELL ⇒ effective BUY
    via_ask = MICRO - comp_bid if comp_bid is not None else None   # comp BUY ⇒ effective SELL
    bids = [b for b in (own_bid, via_bid) if b is not None]
    asks = [a for a in (own_ask, via_ask) if a is not None]
    return (max(bids) if bids else None, min(asks) if asks else None)


def reconcile_market(
    db: DbSession,
    order: OrderService,
    onchain: OnchainAdmin,
    user: User,
    ref,                       # feed.MarketRef (duck-typed to avoid an import cycle)
    snap: BookSnapshot,
    cfg: Settings,
    *,
    cold: bool = False,
) -> dict:
    """Converge the local books (YES + NO complement) to the snapshot.
    Cancels strictly before placements (spec §5). Placements that would cross
    a NON-house order — directly or via the complement token's MINT/MERGE
    boundary — are intentional bot fills (spec §7): they run last, capped at
    cfg.mirror_max_settlements_per_cycle ATTEMPTED settlements per cycle.

    Returned stats: "cancelled" counts ATTEMPTED cancels, "placed" counts
    SUCCESSFUL placements; "deferred" is hot placements skipped over budget
    and "failed" is placements that errored or raised — both mean the cycle
    is incomplete and a later cycle must converge the remainder."""
    # Timing, carried out in the stats the caller already logs. A pass that
    # gets slow should say where without anyone adding a profiler: the same
    # code costs milliseconds against a local anvil and seconds against a
    # remote node, and which segment grew is the whole question.
    t0 = time.perf_counter()
    tokens = [ref.yes_token, ref.no_token]
    with db.read() as conn:
        rows = TableRead.list_live_order_levels(conn, user.api_key, tokens)
        foreign = {t: TableRead.foreign_touch(conn, user.api_key, t) for t in tokens}
    t_db = time.perf_counter() - t0
    current = [
        LiveLevel(r["ORDER_ID"], r["TOKEN_ID"], r["SIDE"],
                  int(r["PRICE"]), int(r["REMAINING_AMOUNT"]))
        for r in rows
    ]
    desired, protect = tier_plan(
        snap, ref.yes_token, ref.no_token,
        hot_depth=cfg.mirror_hot_depth,
        max_depth=cfg.mirror_book_depth,
        cold=cold,
    )
    cancels, places = diff_levels(desired, current, protect=protect)

    # One round trip for all three balances, and the pass keeps them: calm
    # (resting) placements never move them, so this replaces one on-chain read
    # per order — the dominant cost when replicating a deep book.
    t1 = time.perf_counter()
    held, house_usd = _read_balances(onchain, user, ref)
    splits = 0
    try:
        splits = _ensure_inventory(onchain, user, ref, snap, cfg, held)
    except Exception:
        log.exception("inventory split failed for market %s", ref.market_id)
    if splits:
        # A split mints both outcomes and spends collateral, so all three
        # numbers above are stale. Re-read rather than adjust them by the
        # amount asked for: that assumes the transaction moved exactly that
        # and nothing else touched the position, and inventory reading high
        # backs asks the house cannot cover. Splits are rare enough for the
        # second round trip to cost nothing — zero in 179 production passes.
        held, house_usd = _read_balances(onchain, user, ref)
    t_chain = time.perf_counter() - t1
    inventory = dict(held)
    raw_ctf = dict(inventory)
    # KEPT resting SELLs already reserve inventory; only the surplus may back
    # new asks (negative remainder ⇒ the cap places nothing for that token).
    cancel_set = set(cancels)
    for o in current:
        if o.side == "SELL" and o.order_id not in cancel_set:
            inventory[o.token_id] = inventory.get(o.token_id, 0) - o.size_micro
    places = cap_sells_to_inventory(places, inventory)

    # Classify against the EFFECTIVE foreign touch: a foreign order on the
    # complement token is crossable through the matcher's MINT/MERGE paths
    # exactly as if it rested at 1-p on this token.
    eff = {
        ref.yes_token: _merge_touch(foreign[ref.yes_token], foreign[ref.no_token]),
        ref.no_token: _merge_touch(foreign[ref.no_token], foreign[ref.yes_token]),
    }
    calm = [p for p in places if not _crosses(p, *eff[p.token_id])]
    hot = [p for p in places if _crosses(p, *eff[p.token_id])]

    placed = fills = deferred = failed = 0

    def _try_place(p: Placement, *, expect_fill: bool) -> None:
        nonlocal placed, fills, failed
        # Calm placements don't settle this cycle, so the cached balances are
        # exact; a hot placement may settle and shift balances, so pass no hint
        # (None) and let place_order re-read fresh.
        hint = None if expect_fill else (
            house_usd if p.side == "BUY" else raw_ctf.get(p.token_id)
        )
        try:
            resp = order.place_order(user, PlaceOrderRequest(
                token_id=p.token_id, side=p.side,
                price=Decimal(p.price_micro) / MICRO,
                size=Decimal(p.size_micro) / MICRO,
                order_type="GTC",
            ), balance_hint=hint)
        except AdminGasPausedError:
            # The admin wallet is below its stop level: every hot placement is
            # refused until it is refilled. Expected and already logged as an
            # ERROR by the balance loop, so no traceback per placement.
            log.debug("mirror placement paused by the admin gas breaker (market=%s)", ref.market_id)
            failed += 1
            return
        except Exception:
            log.warning("mirror placement raised (market=%s %s@%s)",
                        ref.market_id, p.side, p.price_micro, exc_info=True)
            failed += 1
            return
        if not resp.success:
            log.warning("mirror placement failed (market=%s %s@%s): %s",
                        ref.market_id, p.side, p.price_micro, resp.errorMsg)
            failed += 1
            return
        placed += 1
        if resp.tradeIDs:
            fills += 1
            if not expect_fill:
                log.warning("mirror calm placement filled — a foreign order "
                            "arrived between the touch read and placement "
                            "(benign TOCTOU race; market=%s %s@%s)",
                            ref.market_id, p.side, p.price_micro)

    # Phase 1: atomically cancel the stale orders AND place the new calm
    # (non-crossing) ones in ONE transaction, so a concurrent /book read never
    # catches the empty gap between cancel and replace (which made the book
    # flicker full<->empty during fast re-quoting). Always runs — even with no
    # calm placements it still applies the cancels. Non-crossing by
    # classification, so no matching/settlement.
    calm_reqs = [
        PlaceOrderRequest(
            token_id=p.token_id, side=p.side,
            price=Decimal(p.price_micro) / MICRO,
            size=Decimal(p.size_micro) / MICRO,
            order_type="GTC",
        )
        for p in calm
    ]
    calm_hints = [
        house_usd if p.side == "BUY" else raw_ctf.get(p.token_id)
        for p in calm
    ]
    t2 = time.perf_counter()
    try:
        ids = order.replace_resting_orders(
            user, cancels, calm_reqs, balance_hints=calm_hints)
        placed += len(ids)
        failed += len(calm) - len(ids)  # skipped (unknown token / underfunded)
    except Exception:
        log.warning("mirror batch replace raised (market=%s)",
                    ref.market_id, exc_info=True)
        failed += len(calm)
    t_calm = time.perf_counter() - t2

    # Phase 2: hot placements (real settlements), budgeted by ATTEMPT — a
    # failed settlement still consumed chain time and must count.
    t3 = time.perf_counter()
    attempted = 0
    for p in hot:
        if attempted >= cfg.mirror_max_settlements_per_cycle:
            deferred += 1      # defer to a later cycle — keeps the loop unblocked
            continue
        attempted += 1
        _try_place(p, expect_fill=True)
    t_hot = time.perf_counter() - t3

    return {"placed": placed, "cancelled": len(cancels), "fills": fills,
            "splits": splits, "deferred": deferred, "failed": failed,
            # Milliseconds, rounded: the log line is read by a person.
            "ms_db": round(t_db * 1000),
            "ms_chain": round(t_chain * 1000),
            "ms_calm": round(t_calm * 1000),
            "ms_hot": round(t_hot * 1000),
            "ms_all": round((time.perf_counter() - t0) * 1000)}
