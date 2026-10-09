import asyncio
import logging
import time
from types import SimpleNamespace

import httpx
import pytest
import requests
from fastapi import HTTPException

import agentpit.api.app as app_mod
import agentpit.services.market_service as service
from agentpit.config import Settings
from agentpit.datastructures.market import Market
from agentpit.datastructures.market_state import MarketState
from agentpit.db.table_read import TableRead
from agentpit.polymarket.gamma import _iso
from agentpit.polymarket.polymarket_sync import CoveragePolicy, UpstreamMarket, parse
from agentpit.services.market_service import (
    ChainTask,
    create_markets,
    pay_out,
    transition,
)
from tests.chain_fakes import SkaledAdmin, gamma_row
from tests.db_helpers import fresh_test_db
from tests.fake_skaled import FakeSkaled, make_sender


def _upstream(**over) -> UpstreamMarket:
    m = parse(gamma_row(**over))
    assert isinstance(m, UpstreamMarket)
    return m


def _admin() -> tuple[FakeSkaled, SkaledAdmin]:
    chain = FakeSkaled()
    sender, _, _ = make_sender(chain)
    return chain, SkaledAdmin(chain, sender, sync_chunk_size=8)


def _row(db, market_id) -> Market:
    with db.read() as conn:
        row = TableRead.read_market(conn, market_id)
    assert row is not None
    return row


def test_run_once_takes_the_latest_work_once(monkeypatch):
    seen = []
    monkeypatch.setattr(
        service,
        "create_markets",
        lambda db, admin, markets: seen.append([m.condition for m in markets]),
    )
    monkeypatch.setattr(
        service, "pay_out", lambda db, admin, resolved: seen.append(dict(resolved))
    )
    monkeypatch.setattr(service, "resolutions", lambda http, conditions: {})
    chain = ChainTask(fresh_test_db(), SimpleNamespace(sync_chunk_size=8), Settings())  # type: ignore[arg-type]
    a, b = _upstream(), _upstream()
    chain.admit([a, b])
    chain.admit([a])
    chain.resolve(7, (1, 0))
    chain.resolve(7, (1, 1))

    chain.run_once(httpx.Client())
    chain.run_once(httpx.Client())

    assert seen == [{7: (1, 1)}, [a.condition, b.condition], {}, []]


def test_a_pass_creates_at_most_four_chunks_and_keeps_the_rest_queued(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(
        service,
        "create_markets",
        lambda db, admin, markets: seen.append([m.condition for m in markets]),
    )
    monkeypatch.setattr(service, "pay_out", lambda db, admin, resolved: None)
    monkeypatch.setattr(service, "resolutions", lambda http, conditions: {})
    db = fresh_test_db()
    _, admin = _admin()
    (known,) = create_markets(db, admin, [_upstream()])
    chain = ChainTask(db, SimpleNamespace(sync_chunk_size=2), Settings())  # type: ignore[arg-type]
    backlog = [_upstream() for _ in range(10)]
    carried = _upstream(conditionId=known.polymarket_condition_id)
    chain.admit([carried, *backlog])

    chain.run_once(httpx.Client())
    fresh = _upstream()
    chain.admit([fresh, backlog[9]])
    chain.run_once(httpx.Client())

    assert seen == [
        [m.condition for m in backlog[:8]],
        [m.condition for m in (*backlog[8:], fresh)],
    ]


def test_an_ended_market_resolves_from_the_data_api_in_the_chain_pass():
    db = fresh_test_db()
    _, admin = _admin()
    now = int(time.time())
    ended, stale, live = create_markets(
        db,
        admin,
        [_upstream(endDate=_iso(t)) for t in (now - 60, now - 1200, now + 60)],
    )
    asked: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.params["condition"].split(","))
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "condition_id": m.polymarket_condition_id,
                        "status": "resolved",
                        "payouts": [0, 1_000_000],
                    }
                    for m in (ended, stale, live)
                ]
            },
        )

    ChainTask(db, admin, Settings()).run_once(
        httpx.Client(transport=httpx.MockTransport(handler))
    )

    assert asked == [[ended.polymarket_condition_id]]
    row = _row(db, ended.market_id)
    assert (row.market_state, row.payouts) == (MarketState.RESOLVED, (0, 1))
    assert _row(db, stale.market_id).market_state == MarketState.ACTIVE


async def test_redeem_runs_on_its_own_loop(monkeypatch):
    seen = []
    monkeypatch.setattr(
        service, "redeem_resolved_markets", lambda *args: seen.append(args)
    )

    async def stop(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(service.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await ChainTask("db", "admin", "settings").run_redeem()  # type: ignore[arg-type]
    assert seen == [("db", "admin", "settings")]


def test_created_markets_carry_the_upstream_identity_and_event():
    db = fresh_test_db()
    _, admin = _admin()
    upstream = _upstream()

    (market,) = create_markets(db, admin, [upstream])

    row = _row(db, market.market_id)
    assert row.market_state == MarketState.ACTIVE
    assert (row.question_id, row.polymarket_condition_id) == (
        upstream.condition,
        upstream.condition,
    )
    assert row.polymarket_id == upstream.pm_id
    assert [label for _, label in row.erc1155_tokens] == list(upstream.labels)
    assert row.event_id is not None and upstream.event is not None
    with db.read() as conn:
        event = TableRead.get_event_by_id(conn, row.event_id)
    assert event is not None and event.slug == upstream.event.slug
    assert create_markets(db, admin, [upstream]) == []


def test_pay_out_reports_once_and_resolves_by_the_chain_verdict():
    db = fresh_test_db()
    chain, admin = _admin()
    yes, split = create_markets(db, admin, [_upstream(), _upstream()])
    sent = len(chain.accepted)

    pay_out(db, admin, {yes.market_id: (1, 0), split.market_id: (1, 1)})

    assert len(chain.accepted) == sent + 2
    assert (_row(db, yes.market_id).payouts, _row(db, split.market_id).payouts) == (
        (1, 0),
        (1, 1),
    )
    assert _row(db, split.market_id).market_state == MarketState.RESOLVED
    assert _row(db, split.market_id).winner is None


def test_a_payout_already_on_chain_is_not_sent_again(monkeypatch):
    db = fresh_test_db()
    chain, admin = _admin()
    (market,) = create_markets(db, admin, [_upstream()])
    written = []
    monkeypatch.setattr(service, "transition", lambda *args: written.append(args))

    pay_out(db, admin, {market.market_id: (0, 1)})
    sent = len(chain.accepted)
    pay_out(db, admin, {market.market_id: (0, 1)})

    assert len(chain.accepted) == sent
    assert [args[1:] for args in written] == [
        (market.market_id, MarketState.ACTIVE, MarketState.RESOLVED, (0, 1))
    ] * 2


def test_a_payout_that_did_not_land_leaves_the_row_for_the_next_pass(caplog):
    caplog.set_level(logging.INFO)
    db = fresh_test_db()
    chain, admin = _admin()
    markets = create_markets(db, admin, [_upstream(), _upstream()])
    resolved = {m.market_id: (1, 0) for m in markets}
    chain.batch_errors.extend(
        [requests.ConnectionError("reset by peer"), requests.ConnectionError("again")]
    )

    pay_out(db, admin, resolved)

    assert [_row(db, m.market_id).market_state for m in markets] == [
        MarketState.ACTIVE
    ] * 2
    assert sum("did not land" in r.getMessage() for r in caplog.records) == 2
    pay_out(db, admin, resolved)
    assert [_row(db, m.market_id).payouts for m in markets] == [(1, 0)] * 2


def test_transition_is_compare_and_set():
    db = fresh_test_db()
    _, admin = _admin()
    (market,) = create_markets(db, admin, [_upstream()])

    assert transition(db, market.market_id, MarketState.ACTIVE, MarketState.CLOSED)
    assert not transition(db, market.market_id, MarketState.ACTIVE, MarketState.CLOSED)
    with pytest.raises(HTTPException):
        transition(db, market.market_id, MarketState.CLOSED, MarketState.RESOLVED)
    assert transition(
        db, market.market_id, MarketState.CLOSED, MarketState.RESOLVED, (1, 1)
    )
    row = _row(db, market.market_id)
    assert (row.market_state, row.payouts) == (MarketState.RESOLVED, (1, 1))
    assert row.resolved_at is not None


async def test_the_catalog_loop_admits_then_sweeps_into_the_chain_task(monkeypatch):
    calls = []
    policy = CoveragePolicy(10_000.0, True, frozenset(), frozenset(), (), 10_000.0)
    chain = SimpleNamespace(admit=lambda ms: calls.append(ms), resolve=object())
    monkeypatch.setattr(app_mod, "_carried", lambda db: {db})
    monkeypatch.setattr(app_mod, "walk_admissions", lambda gamma, p, c: [p, c])
    monkeypatch.setattr(
        app_mod, "sweep", lambda db, gamma, resolve: calls.append((db, resolve))
    )

    ticks = iter((None,))

    async def stop(_seconds):
        if next(ticks, True):
            raise asyncio.CancelledError

    monkeypatch.setattr(app_mod.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await app_mod._catalog_loop("db", policy, chain)  # type: ignore[arg-type]
    assert calls == [
        [policy, set()],
        ("db", chain.resolve),
        [policy, {"db"}],
        ("db", chain.resolve),
    ]
