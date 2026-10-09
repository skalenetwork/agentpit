"""The chain task prepares new markets on chain in chunks, after dropping the
ones already carried. One market's chain failure fails only that market; a
whole chunk failing ends the chain work for the rest of the pass."""

import logging
import secrets
from types import SimpleNamespace

import pytest
import requests

import agentpit.services.market_service as service
from agentpit.datastructures.condition_id import ConditionId
from agentpit.db.table_read import TableRead
from agentpit.domain.exceptions import MarketStateError
from agentpit.polymarket.polymarket_sync import UpstreamMarket, parse
from agentpit.services.market_service import PreparedMarkets, create_markets
from tests.chain_fakes import SkaledAdmin, gamma_row
from tests.db_helpers import fresh_test_db
from tests.fake_skaled import FakeSkaled, make_sender


def _pm(question: str) -> UpstreamMarket:
    m = parse(gamma_row(question=question))
    assert isinstance(m, UpstreamMarket)
    return m


def _ids(labels):
    cid = ConditionId("0x" + secrets.token_hex(32))
    return cid, [(str(int(secrets.token_hex(8), 16)), label) for label in labels]


def _warnings(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == service.log.name and r.levelno == logging.WARNING
    ]


def test_new_markets_are_prepared_in_chunks_and_carried_ones_are_not(monkeypatch):
    db = fresh_test_db()
    calls: list[list[bytes]] = []

    def fake_batch(admin, items):
        calls.append([q for q, _ in items])
        return PreparedMarkets([_ids(labels) for _, labels in items])

    monkeypatch.setattr(service, "prepare_markets_on_chain", fake_batch)
    admin = SimpleNamespace(sync_chunk_size=2)
    known = _pm(f"Known {secrets.token_hex(4)}?")
    assert len(create_markets(db, admin, [known])) == 1
    calls.clear()

    fresh = [_pm(f"Chunk {i} {secrets.token_hex(4)}?") for i in range(5)]
    created = create_markets(db, admin, [known, *fresh])

    assert [len(c) for c in calls] == [2, 2, 1]
    assert bytes.fromhex(known.condition[2:]) not in [q for c in calls for q in c]
    assert [m.question for m in created] == [m.question for m in fresh]
    assert created[0].question_id == fresh[0].condition
    assert created[0].polymarket_condition_id == fresh[0].condition
    assert (created[0].polymarket_yes_token_id, created[0].polymarket_no_token_id) == (
        fresh[0].tokens
    )


def test_one_market_failing_on_chain_does_not_stop_the_rest(monkeypatch):
    db = fresh_test_db()
    bad = _pm(f"Bad {secrets.token_hex(4)}?")
    good = [_pm(f"Good {i} {secrets.token_hex(4)}?") for i in range(3)]

    def fake_batch(admin, items):
        return PreparedMarkets(
            [
                (
                    RuntimeError("prepareCondition timed out")
                    if q == bytes.fromhex(bad.condition[2:])
                    else _ids(labels)
                )
                for q, labels in items
            ]
        )

    monkeypatch.setattr(service, "prepare_markets_on_chain", fake_batch)
    created = create_markets(
        db, SimpleNamespace(sync_chunk_size=8), [good[0], bad, good[1], good[2]]
    )
    assert [m.question for m in created] == [m.question for m in good]


def test_one_insert_failing_does_not_stop_the_rest(monkeypatch):
    db = fresh_test_db()
    taken = _ids(["Yes", "No"])
    first, clash, last = (_pm(f"Insert {i} {secrets.token_hex(4)}?") for i in range(3))

    def fake_batch(admin, items):
        return PreparedMarkets(
            [
                (
                    taken
                    if q
                    in (
                        bytes.fromhex(first.condition[2:]),
                        bytes.fromhex(clash.condition[2:]),
                    )
                    else _ids(labels)
                )
                for q, labels in items
            ]
        )

    monkeypatch.setattr(service, "prepare_markets_on_chain", fake_batch)
    created = create_markets(
        db, SimpleNamespace(sync_chunk_size=8), [first, clash, last]
    )
    assert [m.question for m in created] == [first.question, last.question]
    with db.read() as conn:
        assert TableRead.carried_condition_ids(
            conn, [first.condition, clash.condition, last.condition]
        ) == {first.condition, last.condition}


def test_a_chunk_that_raises_ends_the_pass(monkeypatch):
    """A whole chunk failing means the node is out of reach: sending the next
    chunks would only pile up unknowable nonces. The next pass retries."""
    db = fresh_test_db()
    markets = [_pm(f"Raise {i} {secrets.token_hex(4)}?") for i in range(5)]
    calls = {"n": 0}

    def fake_batch(admin, items):
        calls["n"] += 1
        raise ConnectionError("RPC down")

    monkeypatch.setattr(service, "prepare_markets_on_chain", fake_batch)
    with pytest.raises(ConnectionError):
        create_markets(db, SimpleNamespace(sync_chunk_size=2), markets)
    assert calls["n"] == 1
    with db.read() as conn:
        assert (
            TableRead.carried_condition_ids(conn, [m.condition for m in markets])
            == set()
        )


def test_a_chunk_whose_sends_met_an_outage_stops_chain_work(monkeypatch, caplog):
    """The chunk came back, but its sends found the node out of reach (or no
    admin slot free): the next chunks would only add unknowable nonces. What
    the chunk did prepare is inserted; the rest of the pass is one line."""
    caplog.set_level(logging.INFO)
    db = fresh_test_db()
    markets = [_pm(f"Outage {i} {secrets.token_hex(4)}?") for i in range(5)]
    down = requests.ConnectionError("node out of reach")
    calls = {"n": 0}

    def fake_batch(admin, items):
        calls["n"] += 1
        out = PreparedMarkets([_ids(items[0][1])] + [down] * (len(items) - 1))
        out.stop = down
        return out

    monkeypatch.setattr(service, "prepare_markets_on_chain", fake_batch)
    created = create_markets(db, SimpleNamespace(sync_chunk_size=2), markets)
    assert [m.question for m in created] == [markets[0].question]
    assert calls["n"] == 1
    (warning,) = _warnings(caplog)
    assert "node out of reach" in warning and "4 new markets" in warning


def test_an_outage_ends_the_chain_work_of_the_pass_on_a_fake_skaled(caplog):
    """End to end through a real AdminTxSender: the first chunk's batch and
    its resend go unanswered, and no later chunk is sent."""
    caplog.set_level(logging.INFO)
    db = fresh_test_db()
    chain = FakeSkaled()
    sender, _, _ = make_sender(chain)
    admin = SkaledAdmin(chain, sender, sync_chunk_size=2)
    chain.batch_errors.extend(
        [requests.ConnectionError("reset by peer"), requests.ConnectionError("again")]
    )
    markets = [_pm(f"Down {i} {secrets.token_hex(4)}?") for i in range(5)]
    assert create_markets(db, admin, markets) == []
    assert len(chain.batches) == 2
    (warning,) = _warnings(caplog)
    assert "reset by peer" in warning and "5 new markets" in warning


def test_a_market_state_error_names_its_reason_in_the_skip_line(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    db = fresh_test_db()
    bad = _pm(f"Unprepared {secrets.token_hex(4)}?")
    reason = "market not prepared on chain: outcome slots=0"

    monkeypatch.setattr(
        service,
        "prepare_markets_on_chain",
        lambda admin, items: PreparedMarkets([MarketStateError(reason) for _ in items]),
    )
    create_markets(db, SimpleNamespace(sync_chunk_size=8), [bad])
    (warning,) = _warnings(caplog)
    assert "MarketStateError" in warning and reason in warning
