import asyncio
import logging
import threading
import time
from collections.abc import Callable

import httpx

from web3.contract.contract import ContractFunction
from web3.exceptions import TimeExhausted

from agentpit.datastructures.cancel_market_response import CancelMarketResponse
from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.gamma_market import GammaMarket
from agentpit.datastructures.list_markets_response import (
    ListMarketsResponse,
    MarketStatsResponse,
)
from agentpit.datastructures.market import Market, Payouts
from agentpit.datastructures.market_state import MarketState
from agentpit.datastructures.resolve_market_request import ResolveMarketRequest
from agentpit.polymarket.category_resolver import category_rank
from agentpit.polymarket.gamma import to_gamma_market
from agentpit.polymarket.polymarket_sync import (
    RESOLUTION_BATCH,
    UpstreamMarket,
    bind_market_to_upstream_event,
    clob_market,
    fetch_markets,
    resolutions,
)
from agentpit.polymarket.pricing import prices_for_markets
from agentpit.db.session import DbSession
from agentpit.db.table_read import EventFields, MarketFields, TableRead
from agentpit.db.table_write import TableWrite
from agentpit.common import check_state
from agentpit.liquidity import feed
from agentpit.domain.exceptions import (
    InvalidPaginationError,
    MarketNotFoundError,
    MarketStateError,
)
from agentpit.onchain.admin import OnchainAdmin
from agentpit.onchain.ctf_ids import binary_market_ids
from agentpit.onchain.tx_sender import PendingTx, TxDropped, stops_sending
from agentpit.services.event_service import EventService
from agentpit.services.leaderboard_service import touch_holders
from agentpit.services.position_service import PositionService

log = logging.getLogger(__name__)


class MarketService:
    def __init__(
        self,
        db: DbSession,
        onchain: OnchainAdmin,
        excluded_categories: "list[str] | None" = None,
        excluded_tags: "list[str] | None" = None,
    ):
        self._db = db
        self._onchain = onchain
        # Browse-surface filter only — the by-id and by-condition reads below
        # keep resolving an excluded market, so an existing link or a held
        # position never turns into a 404.
        self._excluded_categories = excluded_categories or []
        self._excluded_tags = excluded_tags or []

    def market_stats(self) -> MarketStatsResponse:
        with self._db.read() as conn:
            return MarketStatsResponse(
                active=TableRead.count_open_markets(
                    conn,
                    feed.sided(),
                    excluded_categories=self._excluded_categories,
                    excluded_tags=self._excluded_tags,
                )
            )

    def list_markets(self, limit: int, offset: int) -> ListMarketsResponse:
        if limit < 1 or limit > 1000:
            raise InvalidPaginationError("limit must be between 1 and 1000")
        if offset < 0:
            raise InvalidPaginationError("offset must be non-negative")
        with self._db.read() as conn:
            markets, total = TableRead.list_markets(conn, limit=limit, offset=offset)
        return ListMarketsResponse(
            markets=markets, total=total, limit=limit, offset=offset
        )

    def get_market(self, market_id: int) -> Market:
        with self._db.read() as conn:
            market = TableRead.read_market(conn, market_id)
        if market is None:
            raise MarketNotFoundError(market_id)
        return market

    def list_markets_gamma(
        self,
        *,
        limit: int,
        offset: int,
        market_id: int | None,
        slug: str | None,
        condition_ids: list[str] | None,
        clob_token_ids: list[str] | None,
        polymarket_condition_id: str | None,
    ) -> list[GammaMarket]:
        if limit < 1 or limit > 1000:
            raise InvalidPaginationError("limit must be between 1 and 1000")
        if offset < 0:
            raise InvalidPaginationError("offset must be non-negative")
        with self._db.read() as conn:
            markets = TableRead.list_markets_filtered(
                conn,
                limit=limit,
                offset=offset,
                market_id=market_id,
                slug=slug,
                condition_ids=condition_ids,
                clob_token_ids=clob_token_ids,
                polymarket_condition_id=polymarket_condition_id,
                excluded_categories=self._excluded_categories,
                excluded_tags=self._excluded_tags,
            )
            prices = prices_for_markets(conn, markets)
        return [to_gamma_market(m, prices.get(m.market_id)) for m in markets]

    def get_market_gamma(self, market_id: int) -> GammaMarket:
        with self._db.read() as conn:
            market = TableRead.read_market(conn, market_id)
            if market is None:
                raise MarketNotFoundError(market_id)
            prices = prices_for_markets(conn, [market])
        return to_gamma_market(market, prices.get(market.market_id))

    def create_market(self, payload: CreateMarketRequest) -> Market:
        # Local creation runs on-chain prepareCondition + registerToken so that
        # subsequent fills can settle.
        if payload.condition_id is None and payload.outcome_labels is not None:
            self._prepare_market_on_chain(payload)

        with self._db.write() as conn:
            market = TableWrite.create_market(
                conn, payload, is_polygon_market=payload.condition_id is not None
            )
        # Enforce the "every market belongs to an event" invariant immediately —
        # without this, locally-created orphan markets would be invisible on
        # the home page until the next process restart (which runs the
        # startup auto-wrap).
        if market.event_id is None:
            EventService(self._db).wrap_market_in_singleton_event_if_needed(
                market.market_id,
                category=payload.category,
            )
            with self._db.read() as conn:
                refreshed = TableRead.read_market(conn, market.market_id)
                if refreshed is not None:
                    return refreshed
        return market

    def _prepare_market_on_chain(self, payload: CreateMarketRequest) -> None:
        """Run prepareCondition + registerToken and back-fill payload fields."""
        condition_id, erc1155_tokens = prepare_market_on_chain(
            self._onchain,
            bytes.fromhex(payload.question_id[2:]),
            payload.outcome_labels or [],
        )
        payload.condition_id = condition_id
        payload.erc1155_tokens = erc1155_tokens

    def activate_market(self, market_id: int) -> Market:
        return self._transition(market_id, MarketState.DRAFT, MarketState.ACTIVE)

    def close_market(self, market_id: int) -> Market:
        return self._transition(market_id, MarketState.ACTIVE, MarketState.CLOSED)

    def cancel_market(self, market_id: int) -> CancelMarketResponse:
        state = self.get_market(market_id).market_state
        if state in (MarketState.RESOLVED, MarketState.CANCELLED):
            raise MarketStateError(
                f"Market {market_id} is already {state.value.lower()}"
            )
        return CancelMarketResponse(
            market_id=market_id,
            message="Market cancelled successfully",
            refunds_processed=0,
            market=self._transition(market_id, state, MarketState.CANCELLED),
        )

    def resolve_market(self, market_id: int, payload: ResolveMarketRequest) -> Market:
        market = self.get_market(market_id)
        if market.market_state == MarketState.RESOLVED:
            raise MarketStateError(f"Market {market_id} is already resolved")
        if payload.winning_outcome_index > 1:
            raise MarketStateError(
                f"Invalid winning_outcome_index {payload.winning_outcome_index}: "
                "a market has outcomes 0 and 1"
            )
        payouts: Payouts = (
            int(payload.winning_outcome_index == 0),
            int(payload.winning_outcome_index == 1),
        )
        pay_out(self._db, self._onchain, {market_id: payouts})
        resolved = self.get_market(market_id)
        if resolved.market_state != MarketState.RESOLVED:
            raise MarketStateError(
                f"Market {market_id} did not resolve (state {resolved.market_state.value}); "
                "a market resolves from ACTIVE or CLOSED once its payouts are on chain"
            )
        return resolved

    def _transition(
        self, market_id: int, expected: MarketState, new: MarketState
    ) -> Market:
        if not transition(self._db, market_id, expected, new):
            current = self.get_market(market_id).market_state.value
            raise MarketStateError(
                f"Market {market_id} is not in {expected.value} state (current: {current})"
            )
        return self.get_market(market_id)


_market_locks: dict[int, threading.Lock] = {}


def market_lock(market_id: int) -> threading.Lock:
    return _market_locks.setdefault(market_id, threading.Lock())


def transition(
    db: DbSession,
    market_id: int,
    expected: MarketState,
    new: MarketState,
    payouts: Payouts | None = None,
) -> bool:
    check_state(new != MarketState.RESOLVED or payouts is not None)
    with market_lock(market_id), db.write() as conn:
        if not TableWrite.set_market_state(conn, market_id, expected, new, payouts):
            return False
        cancelled = TableWrite.cancel_all_market_orders(conn, market_id)
    log.info(
        "market %d %s -> %s, %d live orders cancelled",
        market_id,
        expected.value,
        new.value,
        cancelled,
    )
    if new == MarketState.RESOLVED:
        touch_holders()
    return True


# A chunk of up to ~64 transactions; blocks come every 1-2 s under load.
_PREPARE_WAIT_S = 120


class PreparedMarkets(list[tuple[ConditionId, list[tuple[str, str]]] | Exception]):
    """What `prepare_markets_on_chain` returns: one result per item, as a
    plain list, plus `stop`. `stop` is the first error of this chunk's sends
    that said the node is out of reach or no admin slot freed up
    (`stops_sending`), what `submit_many` raised, or the first receipt wait
    that timed out. It is set even when the market it hit shows another error
    (a failed registerToken is judged by the chain read), so the sync can end
    its chain work for the pass."""

    stop: Exception | None = None


class _ConditionPlan:
    """On-chain work for one condition id, shared by every input that asked
    for the same question id."""

    def __init__(self, question_id: bytes, condition_id: bytes, tokens: list[int]):
        self.question_id = question_id
        self.condition_id = condition_id
        self.tokens = tokens
        self.indexes: list[int] = []
        self.pending: list[PendingTx] = []
        self.error: Exception | None = None


def prepare_markets_on_chain(
    admin: OnchainAdmin, items: list[tuple[bytes, list[str]]]
) -> PreparedMarkets:
    """Prepare many binary markets on the local CTF + Exchange at once.

    For each `(question_id, outcome_labels)`: `prepareCondition` if the condition
    is new, `registerToken` if its tokens are not registered, then a check that
    both persisted. Ids are derived off-chain, chain state is read in JSON-RPC
    batches, and every transaction is broadcast before any receipt is awaited,
    so a chunk lands in one or two blocks instead of two blocks per market.

    Returns one result per item, in order: `(condition_id, [(token_id,
    label), ...])`, or the exception that market failed with. Identical
    question ids share one condition and get the same ids.
    Every transaction goes out in one `submit_many`: if it raises, every
    market still needing a transaction gets that exception; a failed
    prepareCondition fails its market. A failed state read raises for the
    whole batch. The result's `stop` says whether the sends or their receipts
    met an outage (`PreparedMarkets`).
    """
    results: list[tuple[ConditionId, list[tuple[str, str]]] | Exception | None] = [
        None
    ] * len(items)
    plans: dict[bytes, _ConditionPlan] = {}
    oracle = admin.oracle_address
    collateral = admin.collateral_address
    for i, (question_id, labels) in enumerate(items):
        if len(labels) != 2:
            results[i] = MarketStateError(
                "exchange.registerToken only supports binary (YES/NO) markets"
            )
            continue
        condition_id, tokens = binary_market_ids(oracle, collateral, question_id)
        plan = plans.setdefault(
            condition_id, _ConditionPlan(question_id, condition_id, tokens)
        )
        plan.indexes.append(i)

    stop = _run_condition_plans(admin, list(plans.values())) if plans else None

    for plan in plans.values():
        for i in plan.indexes:
            if plan.error is not None:
                results[i] = plan.error
                continue
            labels = items[i][1]
            results[i] = (
                ConditionId("0x" + plan.condition_id.hex()),
                list(zip((str(t) for t in plan.tokens), labels)),
            )
    assert all(r is not None for r in results)
    prepared = PreparedMarkets(results)  # type: ignore[arg-type]
    prepared.stop = stop
    return prepared


def _run_condition_plans(
    admin: OnchainAdmin, plans: list[_ConditionPlan]
) -> Exception | None:
    """Send, await and check every plan's transactions; each plan's error is
    set on it. Returns the first send error that `stops_sending`, what
    `submit_many` raised, or the first receipt wait that timed out, else
    None."""
    stop: Exception | None = None
    states = admin.read_market_states([(p.condition_id, p.tokens) for p in plans])
    calls: list[tuple[ContractFunction, int]] = []
    owners: list[tuple[_ConditionPlan, bool]] = []  # (plan, is its prepare)
    for plan, (slots, comp_a, comp_b) in zip(plans, states, strict=True):
        if slots not in (0, 2):
            plan.error = MarketStateError(
                f"condition already prepared with {slots} slots, expected 2"
            )
            continue
        if slots == 0:
            calls.append(
                admin.prepare_condition_call(admin.oracle_address, plan.question_id, 2)
            )
            owners.append((plan, True))
        if comp_a == 0 or comp_b == 0:
            # registerToken never looks at the CTF, so it may follow its own
            # prepareCondition into the same batch and the same block.
            calls.append(
                admin.register_token_call(
                    plan.tokens[0], plan.tokens[1], plan.condition_id
                )
            )
            owners.append((plan, False))

    if calls:
        # One JSON-RPC batch for the whole chunk. If it raises, the error is
        # never about one market (the gas is static, nothing is estimated):
        # the node is out of reach or the admin nonce stream is in trouble.
        # Every market that needed a transaction fails with it and the next
        # sync pass retries.
        try:
            submitted = admin.submit_many(calls)
        except Exception as exc:
            for plan, _ in owners:
                plan.error = exc
            stop = exc
        else:
            for (plan, is_prepare), outcome in zip(owners, submitted, strict=True):
                if not isinstance(outcome, Exception):
                    plan.pending.append(outcome)
                    continue
                if stop is None and stops_sending(outcome):
                    stop = outcome
                if is_prepare:
                    plan.error = outcome
                # A failed registerToken is left to the verdict read below:
                # another path may have registered the pair, or the
                # transaction, its answer lost, may land after all.

    sent = [tx for plan in plans for tx in plan.pending]
    if sent:
        outcomes = admin.wait_all(sent, timeout=_PREPARE_WAIT_S)
        for tx, outcome in zip(sent, outcomes, strict=True):
            if isinstance(outcome, Exception):
                log.warning(
                    "market tx 0x%s (nonce %d) not confirmed: %s",
                    tx.tx_hash.hex(),
                    tx.nonce,
                    outcome,
                )
            if stop is None and isinstance(outcome, (TimeExhausted, TxDropped)):
                stop = outcome

    todo = [p for p in plans if p.error is None]
    if not todo:
        return stop
    # The chain state is the verdict, not the receipts:
    # registerToken reverts AlreadyRegistered when another path registered the
    # pair first, and a transaction that timed out here may still land (the
    # next sync pass then finds the market prepared and skips the sends).
    for plan, (slots, comp_a, comp_b) in zip(
        todo,
        admin.read_market_states([(p.condition_id, p.tokens) for p in todo]),
        strict=True,
    ):
        if slots != 2 or comp_a == 0 or comp_b == 0:
            plan.error = MarketStateError(
                f"market not prepared on chain for condition "
                f"0x{plan.condition_id.hex()}: outcome slots={slots}, "
                f"registry[{plan.tokens[0]}].complement={comp_a}, "
                f"registry[{plan.tokens[1]}].complement={comp_b}"
            )
            continue
        log.info(
            "market prepared on-chain: condition_id=0x%s tokens=%s",
            plan.condition_id.hex(),
            plan.tokens,
        )
    return stop


def prepare_market_on_chain(
    admin: OnchainAdmin, question_id: bytes, outcome_labels: list[str]
) -> tuple[ConditionId, list[tuple[str, str]]]:
    """Prepare one binary market on the local CTF + Exchange.

    `prepare_markets_on_chain` for a single item: used by local market
    creation (`MarketService.create_market`).
    """
    (result,) = prepare_markets_on_chain(admin, [(question_id, outcome_labels)])
    if isinstance(result, Exception):
        raise result
    return result


_CHAIN_SECONDS = 5.0
_REDEEM_BUDGET = 10
_RESOLVING_SECONDS = 900
_CREATE_CHUNKS = 4


class ChainTask:
    def __init__(self, db: DbSession, admin: OnchainAdmin):
        self._db = db
        self._admin = admin
        self._lock = threading.Lock()
        self._admitted: dict[str, UpstreamMarket] = {}
        self._resolved: dict[int, Payouts] = {}

    def admit(self, markets: list[UpstreamMarket]) -> None:
        with self._lock:
            self._admitted.update((m.condition, m) for m in markets)

    def resolve(self, market_id: int, payouts: Payouts) -> None:
        with self._lock:
            self._resolved[market_id] = payouts

    async def run(self) -> None:
        with httpx.Client(timeout=10) as data:
            while True:
                try:
                    await asyncio.to_thread(self.run_once, data)
                except Exception:
                    log.exception("Chain task pass failed")
                await asyncio.sleep(_CHAIN_SECONDS)

    async def run_redeem(self) -> None:
        while True:
            try:
                await asyncio.to_thread(
                    redeem_resolved_markets, self._db, self._admin, _REDEEM_BUDGET
                )
            except Exception:
                log.exception("Redeem pass failed")
            await asyncio.sleep(_CHAIN_SECONDS)

    def run_once(self, data: httpx.Client) -> None:
        with self._lock:
            admitted, self._admitted = list(self._admitted.values()), {}
            resolved, self._resolved = self._resolved, {}
        now = int(time.time())
        with self._db.read() as conn:
            ended = TableRead.ended_unresolved(
                conn, now - _RESOLVING_SECONDS, now, RESOLUTION_BATCH
            )
        resolved |= {ended[c]: p for c, p in resolutions(data, list(ended)).items()}
        pay_out(self._db, self._admin, resolved)
        with self._db.read() as conn:
            carried = TableRead.carried_condition_ids(
                conn, [m.condition for m in admitted]
            )
        new = [m for m in admitted if m.condition not in carried]
        cap = _CREATE_CHUNKS * self._admin.sync_chunk_size
        with self._lock:
            self._admitted = {m.condition: m for m in new[cap:]} | self._admitted
        create_markets(self._db, self._admin, new[:cap])


def create_markets(
    db: DbSession, admin: OnchainAdmin, markets: list[UpstreamMarket]
) -> list[Market]:
    with db.read() as conn:
        carried = TableRead.carried_condition_ids(conn, [m.condition for m in markets])
    new = [m for m in markets if m.condition not in carried]
    created: list[Market] = []
    for start in range(0, len(new), admin.sync_chunk_size):
        batch = new[start : start + admin.sync_chunk_size]
        prepared = prepare_markets_on_chain(
            admin, [(bytes.fromhex(m.condition[2:]), list(m.labels)) for m in batch]
        )
        for m, outcome in zip(batch, prepared, strict=True):
            if isinstance(outcome, Exception):
                if prepared.stop is None:
                    log.warning(
                        "Skip %r (%s: %s)", m.question, type(outcome).__name__, outcome
                    )
                continue
            condition_id, tokens = outcome
            try:
                with db.write() as conn:
                    market = TableWrite.create_market(
                        conn,
                        CreateMarketRequest(
                            question=m.question,
                            description=m.description,
                            erc1155_tokens=tokens,
                            slug=m.slug,
                            start_date=m.start_date,
                            end_date=m.end_date,
                            polymarket_id=m.pm_id,
                            polymarket_condition_id=m.condition,
                            polymarket_yes_token_id=m.tokens[0],
                            polymarket_no_token_id=m.tokens[1],
                            condition_id=condition_id,
                            question_id=m.condition,
                            state=MarketState.ACTIVE,
                            outcome_label=m.label,
                            icon_url=m.icon,
                        ),
                        True,
                    )
                    bind_market_to_upstream_event(conn, market.market_id, m)
            except Exception:
                log.exception("Insert of %r failed", m.question)
                continue
            created.append(market)
        if prepared.stop is not None:
            log.warning(
                "Chain step stopped (%s: %s); %d new markets left for the next pass",
                type(prepared.stop).__name__,
                prepared.stop,
                len(new) - len(created),
            )
            break
    if created:
        log.info("Created %d markets", len(created))
    return created


def pay_out(db: DbSession, admin: OnchainAdmin, resolved: dict[int, Payouts]) -> None:
    with db.read() as conn:
        markets = [
            m
            for market_id in resolved
            if (m := TableRead.read_market(conn, market_id)) is not None
            and m.market_state in (MarketState.ACTIVE, MarketState.CLOSED)
        ]
    for start in range(0, len(markets), admin.sync_chunk_size):
        batch = markets[start : start + admin.sync_chunk_size]
        conditions = [bytes.fromhex(m.condition_id.value[2:]) for m in batch]
        unpaid = [
            m
            for m, d in zip(batch, admin.payout_denominators(conditions), strict=True)
            if d == 0
        ]
        if unpaid:
            sent = admin.submit_many(
                [
                    admin.report_payouts_call(
                        bytes.fromhex(m.question_id[2:]), resolved[m.market_id]
                    )
                    for m in unpaid
                ]
            )
            admin.wait_all(
                [tx for tx in sent if isinstance(tx, PendingTx)],
                timeout=_PREPARE_WAIT_S,
            )
        for m, d in zip(batch, admin.payout_denominators(conditions), strict=True):
            if d == 0:
                log.warning("reportPayouts for market %s did not land", m.market_id)
            else:
                transition(
                    db,
                    m.market_id,
                    m.market_state,
                    MarketState.RESOLVED,
                    resolved[m.market_id],
                )


def redeem_resolved_markets(db: DbSession, admin: OnchainAdmin, limit: int) -> int:
    positions = PositionService(db, admin)
    redeemed = 0
    with db.read() as conn:
        markets = TableRead.list_resolved_unredeemed_markets(conn, limit)
    for market in markets:
        tokens = [token for token, _ in market.erc1155_tokens]
        with db.read() as conn:
            users = [
                TableRead.get_user_by_api_key(conn, api_key)
                for api_key in TableRead.list_participant_api_keys_for_market(
                    conn, market.market_id, tokens
                )
            ]
        failed = False
        for user in users:
            if (
                user is None
                or not user.auto_redeem
                or not any(
                    admin.ctf_balances(user.eth_address, [int(t) for t in tokens])
                )
            ):
                continue
            try:
                positions.redeem(user, market.market_id)
                redeemed += 1
            except Exception:
                failed = True
                log.exception(
                    "auto-redeem failed for %s on market %s",
                    user.eth_address,
                    market.market_id,
                )
        if not failed:
            with db.write() as conn:
                TableWrite.mark_fully_redeemed(conn, market.market_id)
    return redeemed


def sweep(
    db: DbSession, http: httpx.Client, resolve: Callable[[int, Payouts], None]
) -> None:
    with db.read() as conn:
        carried = TableRead.list_carried(conn)
    upstream = fetch_markets(http, [c.pm_condition for c in carried])
    markets: list[tuple[int, MarketFields]] = []
    tags: list[tuple[int, list[tuple[str, str]]]] = []
    events: dict[int, EventFields] = {}
    closed = reopened = resolving = 0
    for c in carried:
        m = upstream.get(c.pm_condition)
        if m is None:
            continue
        fresh = MarketFields(
            m.slug,
            m.question,
            m.description,
            m.end_date,
            m.icon,
            m.label,
            m.price_change_24h,
        )
        fields = c.fields._replace(
            **{k: v for k, v in fresh._asdict().items() if v is not None}
        )
        if fields != c.fields:
            markets.append((c.market_id, fields))
        if frozenset(m.tags) != c.tags:
            tags.append((c.market_id, list(m.tags)))
        e = m.event
        if (
            c.event_id is not None
            and c.event is not None
            and e is not None
            and e.polymarket_event_id == c.pm_event_id
        ):
            stored = events.get(c.event_id, c.event)
            fresh_event = EventFields(
                e.slug,
                e.title,
                e.icon_url,
                e.start_date,
                e.end_date,
                e.volume_24hr,
                e.volume,
                e.liquidity,
                e.competitive,
                e.start_time,
                e.game_id,
                e.series_slug,
                min(stored.category, m.category, key=category_rank),
            )
            event = stored._replace(
                **{k: v for k, v in fresh_event._asdict().items() if v is not None}
            )
            if event != c.event:
                events[c.event_id] = event
        if c.state == MarketState.ACTIVE and (m.closed is True or m.accepting is False):
            closed += transition(
                db, c.market_id, MarketState.ACTIVE, MarketState.CLOSED
            )
        elif (
            c.state == MarketState.CLOSED
            and m.closed is False
            and m.accepting is True
            and (clob := clob_market(http, c.pm_condition)) is not None
            and clob.accepting
        ):
            reopened += transition(
                db, c.market_id, MarketState.CLOSED, MarketState.ACTIVE
            )
        if m.payouts is not None:
            resolve(c.market_id, m.payouts)
            resolving += 1
    if markets or tags or events:
        with db.write() as conn:
            TableWrite.update_carried(conn, markets, list(events.items()))
            for market_id, market_tags in tags:
                TableWrite.replace_market_tags(
                    conn, market_id=market_id, tags=market_tags
                )
    log.info(
        "sweep: %d carried, %d missing upstream, %d markets, %d tag sets and %d events "
        "refreshed, %d closed, %d reopened, %d resolving",
        len(carried),
        sum(c.pm_condition not in upstream for c in carried),
        len(markets),
        len(tags),
        len(events),
        closed,
        reopened,
        resolving,
    )
