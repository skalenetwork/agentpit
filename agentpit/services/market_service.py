import logging

from eth_utils import keccak

from agentpit.datastructures.cancel_market_response import CancelMarketResponse
from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.gamma_market import GammaMarket
from agentpit.datastructures.list_markets_response import (
    ListMarketsResponse,
    MarketStatsResponse,
)
from agentpit.datastructures.market import Market
from agentpit.datastructures.resolve_market_request import ResolveMarketRequest
from agentpit.polymarket.gamma import to_gamma_market
from agentpit.polymarket.pricing import prices_for_markets
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import (
    InvalidPaginationError,
    MarketNotFoundError,
    MarketStateError,
)
from agentpit.onchain.admin import OnchainAdmin
from agentpit.onchain.ctf_ids import binary_market_ids
from agentpit.onchain.tx_sender import PendingTx
from agentpit.services.event_service import EventService

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
                active=TableRead.count_active_markets(
                    conn,
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
        # subsequent fills can settle. The Polymarket sync path supplies
        # condition_id directly and skips this whole branch.
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
            self._onchain, payload.question, payload.outcome_labels or []
        )
        payload.condition_id = condition_id
        payload.erc1155_tokens = erc1155_tokens

    def activate_market(self, market_id: int) -> Market:
        with self._db.write() as conn:
            try:
                return TableWrite.activate_market(conn, market_id)
            except ValueError as e:
                raise MarketStateError(str(e)) from e

    def close_market(self, market_id: int) -> Market:
        with self._db.write() as conn:
            try:
                return TableWrite.close_market(conn, market_id)
            except ValueError as e:
                raise MarketStateError(str(e)) from e

    def cancel_market(self, market_id: int) -> CancelMarketResponse:
        with self._db.write() as conn:
            try:
                market, refunds_processed = TableWrite.cancel_market(conn, market_id)
            except ValueError as e:
                raise MarketStateError(str(e)) from e
        return CancelMarketResponse(
            market_id=market.market_id,
            message="Market cancelled successfully",
            refunds_processed=refunds_processed,
            market=market,
        )

    def resolve_market(self, market_id: int, payload: ResolveMarketRequest) -> Market:
        with self._db.write() as conn:
            market = TableRead.read_market(conn, market_id)
            if market is None:
                raise MarketNotFoundError(market_id)
            try:
                return TableWrite.resolve_market(
                    conn,
                    market_id=market_id,
                    winning_outcome_index=payload.winning_outcome_index,
                )
            except ValueError as e:
                raise MarketStateError(str(e)) from e


# A chunk of up to ~64 transactions; blocks come every 1-2 s under load.
_PREPARE_WAIT_S = 120


class _ConditionPlan:
    """On-chain work for one condition id, shared by every input that asked
    for the same question."""

    def __init__(self, question_id: bytes, condition_id: bytes, tokens: list[int]):
        self.question_id = question_id
        self.condition_id = condition_id
        self.tokens = tokens
        self.indexes: list[int] = []
        self.pending: list[PendingTx] = []
        self.error: Exception | None = None


def prepare_markets_on_chain(
    admin: OnchainAdmin, items: list[tuple[str, list[str]]]
) -> list[tuple[ConditionId, list[tuple[str, str]]] | Exception]:
    """Prepare many binary markets on the local CTF + Exchange at once.

    For each `(question, outcome_labels)`: `prepareCondition` if the condition
    is new, `registerToken` if its tokens are not registered, then a check that
    both persisted. Ids are derived off-chain, chain state is read in JSON-RPC
    batches, and every transaction is broadcast before any receipt is awaited,
    so a chunk lands in one or two blocks instead of two blocks per market.

    Returns one result per item, in order: `(condition_id, [(token_id,
    label), ...])`, or the exception that market failed with. Identical
    questions share one condition and get the same ids, as they always did.
    A submit that raises stops the sending: every market still needing a
    transaction gets that same exception. A failed state read raises for the
    whole batch.
    """
    results: list[tuple[ConditionId, list[tuple[str, str]]] | Exception | None] = [
        None
    ] * len(items)
    plans: dict[bytes, _ConditionPlan] = {}
    oracle = admin.oracle_address
    collateral = admin.collateral_address
    for i, (question, labels) in enumerate(items):
        if len(labels) != 2:
            results[i] = MarketStateError(
                "exchange.registerToken only supports binary (YES/NO) markets"
            )
            continue
        question_id = keccak(text=question)
        condition_id, tokens = binary_market_ids(oracle, collateral, question_id)
        plan = plans.setdefault(
            condition_id, _ConditionPlan(question_id, condition_id, tokens)
        )
        plan.indexes.append(i)

    if plans:
        _run_condition_plans(admin, list(plans.values()))

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
    return results  # type: ignore[return-value]


def _run_condition_plans(admin: OnchainAdmin, plans: list[_ConditionPlan]) -> None:
    states = admin.read_market_states([(p.condition_id, p.tokens) for p in plans])
    # The first submit that raises ends the sending. A submit error is never
    # about one market (the gas is static, nothing is estimated): the node is
    # out of reach or the admin nonce stream is in trouble. Sending the rest
    # would only add sends whose answers are lost, each leaving a nonce gap
    # behind. Every market that still needs a transaction fails with that same
    # error and the next sync pass retries it; what was sent is still awaited
    # and verified below.
    failure: Exception | None = None
    for plan, (slots, comp_a, comp_b) in zip(plans, states, strict=True):
        if slots not in (0, 2):
            plan.error = MarketStateError(
                f"condition already prepared with {slots} slots, expected 2"
            )
            continue
        needs_prepare = slots == 0
        needs_register = comp_a == 0 or comp_b == 0
        if failure is not None:
            if needs_prepare or needs_register:
                plan.error = failure
            continue
        try:
            if needs_prepare:
                plan.pending.append(
                    admin.submit_prepare_condition(
                        admin.oracle_address, plan.question_id, 2
                    )
                )
            if needs_register:
                # registerToken never looks at the CTF, so it may follow its
                # own prepareCondition into the same block.
                plan.pending.append(
                    admin.submit_register_token(
                        plan.tokens[0], plan.tokens[1], plan.condition_id
                    )
                )
        except Exception as exc:
            plan.error = failure = exc

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

    todo = [p for p in plans if p.error is None]
    if not todo:
        return
    # The chain state is the verdict, not the receipts, exactly as before:
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


def prepare_market_on_chain(
    admin: OnchainAdmin, question: str, outcome_labels: list[str]
) -> tuple[ConditionId, list[tuple[str, str]]]:
    """Prepare one binary market on the local CTF + Exchange.

    `prepare_markets_on_chain` for a single item: used by local market
    creation (`MarketService.create_market`) and the single-market sync path.
    """
    (result,) = prepare_markets_on_chain(admin, [(question, outcome_labels)])
    if isinstance(result, Exception):
        raise result
    return result
