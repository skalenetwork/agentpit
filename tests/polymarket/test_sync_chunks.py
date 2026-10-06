"""The sync prepares new markets on chain in chunks, after classifying every
candidate. One market's chain failure fails only that market; a whole chunk
failing ends the chain work for the rest of the pass."""

import logging
import secrets
from types import SimpleNamespace

import requests

import agentpit.polymarket.polymarket_sync as sync
from agentpit.datastructures.condition_id import ConditionId
from agentpit.domain.exceptions import MarketStateError
from tests.chain_fakes import SkaledAdmin
from tests.db_helpers import fresh_test_db
from tests.fake_skaled import FakeSkaled, make_sender


def _pm(question: str) -> dict:
    return {
        "id": int(secrets.token_hex(4), 16),
        "conditionId": "0x" + secrets.token_hex(32),
        "question": question,
        "description": "d",
        "slug": f"chunks-{secrets.token_hex(4)}",
        "startDate": "2020-01-01T00:00:00Z",
        "endDate": "2030-01-02T00:00:00Z",
        "active": True,
        "closed": False,
        "tokens": [
            {"token_id": str(int(secrets.token_hex(8), 16)), "outcome": "Yes"},
            {"token_id": str(int(secrets.token_hex(8), 16)), "outcome": "No"},
        ],
    }


def _ids(labels):
    cid = ConditionId("0x" + secrets.token_hex(32))
    return cid, [(str(int(secrets.token_hex(8), 16)), label) for label in labels]


def test_new_markets_are_prepared_in_chunks_and_known_ones_are_not(monkeypatch):
    db = fresh_test_db()
    calls: list[list[str]] = []

    def fake_batch(admin, items):
        calls.append([q for q, _ in items])
        return [_ids(labels) for _, labels in items]

    monkeypatch.setattr(sync, "prepare_markets_on_chain", fake_batch)
    admin = SimpleNamespace(sync_chunk_size=2)
    known = _pm(f"Known {secrets.token_hex(4)}?")
    with db.write() as conn:
        assert len(sync.create_polymarket_markets_if_needed(conn, [known], admin)) == 1
    calls.clear()

    fresh = [_pm(f"Chunk {i} {secrets.token_hex(4)}?") for i in range(5)]
    with db.write() as conn:
        created = sync.create_polymarket_markets_if_needed(conn, [known, *fresh], admin)

    assert [len(c) for c in calls] == [2, 2, 1]
    assert known["question"] not in [q for c in calls for q in c]
    assert [m.question for m in created] == [pm["question"] for pm in fresh]


def test_one_market_failing_on_chain_does_not_stop_the_rest(monkeypatch):
    db = fresh_test_db()
    bad = _pm(f"Bad {secrets.token_hex(4)}?")
    good = [_pm(f"Good {i} {secrets.token_hex(4)}?") for i in range(3)]

    def fake_batch(admin, items):
        return [
            RuntimeError("prepareCondition timed out") if q == bad["question"] else _ids(labels)
            for q, labels in items
        ]

    monkeypatch.setattr(sync, "prepare_markets_on_chain", fake_batch)
    with db.write() as conn:
        created = sync.create_polymarket_markets_if_needed(
            conn, [good[0], bad, good[1], good[2]], SimpleNamespace(sync_chunk_size=8)
        )
    assert [m.question for m in created] == [pm["question"] for pm in good]


def _sync_lines(caplog) -> tuple[list[str], str]:
    """(warning lines, the closing "Synced ..." line) of the sync logger."""
    records = [r for r in caplog.records if r.name == sync.logger.name]
    warnings = [r.getMessage() for r in records if r.levelno == logging.WARNING]
    (summary,) = [r.getMessage() for r in records if "Synced" in r.getMessage()]
    return warnings, summary


def test_a_chunk_that_raises_stops_chain_work_for_the_rest_of_the_pass(
    monkeypatch, caplog
):
    """A whole chunk failing means the node is out of reach: sending the next
    chunks would only pile up unknowable nonces. One warning line, every
    market counted failed, the next pass retries."""
    caplog.set_level(logging.INFO)
    db = fresh_test_db()
    markets = [_pm(f"Raise {i} {secrets.token_hex(4)}?") for i in range(5)]
    calls = {"n": 0}

    def fake_batch(admin, items):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("RPC down")
        return [_ids(labels) for _, labels in items]

    monkeypatch.setattr(sync, "prepare_markets_on_chain", fake_batch)
    with db.write() as conn:
        created = sync.create_polymarket_markets_if_needed(
            conn, markets, SimpleNamespace(sync_chunk_size=2)
        )
    assert created == []
    assert calls["n"] == 1  # the later chunks never reached the chain
    warnings, summary = _sync_lines(caplog)
    assert len(warnings) == 1
    assert "RPC down" in warnings[0] and "5" in warnings[0]
    assert summary.endswith("(5 failed)")


def test_a_chunk_whose_sends_met_an_outage_stops_chain_work(monkeypatch, caplog):
    """The chunk came back, but its sends found the node out of reach (or no
    admin slot free): the next chunks would only add unknowable nonces. What
    the chunk did prepare is inserted; the rest of the pass is one line."""
    from agentpit.services.market_service import PreparedMarkets

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

    monkeypatch.setattr(sync, "prepare_markets_on_chain", fake_batch)
    with db.write() as conn:
        created = sync.create_polymarket_markets_if_needed(
            conn, markets, SimpleNamespace(sync_chunk_size=2)
        )
    assert [m.question for m in created] == [markets[0]["question"]]
    assert calls["n"] == 1  # the later chunks never reached the chain
    warnings, summary = _sync_lines(caplog)
    assert len(warnings) == 1
    assert "node out of reach" in warnings[0] and "4 new markets" in warnings[0]
    assert summary.endswith("(4 failed)")


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
    with db.write() as conn:
        created = sync.create_polymarket_markets_if_needed(conn, markets, admin)
    assert created == []
    assert len(chain.batches) == 2  # the first chunk's batch and its resend
    warnings, summary = _sync_lines(caplog)
    assert len(warnings) == 1
    assert "reset by peer" in warnings[0] and "5 new markets" in warnings[0]
    assert summary.endswith("(5 failed)")


def test_the_same_market_twice_in_one_pass_is_created_once(monkeypatch, caplog):
    """Gamma's pagination over a live volume sort can return a market twice.
    The second copy is not a new market."""
    caplog.set_level(logging.INFO)
    db = fresh_test_db()
    calls: list[list[str]] = []
    ids: dict[str, tuple] = {}

    def fake_batch(admin, items):
        calls.append([q for q, _ in items])
        return [ids.setdefault(q, _ids(labels)) for q, labels in items]

    monkeypatch.setattr(sync, "prepare_markets_on_chain", fake_batch)
    pm = _pm(f"Twice {secrets.token_hex(4)}?")
    other = _pm(f"Once {secrets.token_hex(4)}?")
    with db.write() as conn:
        created = sync.create_polymarket_markets_if_needed(
            conn, [pm, other, dict(pm)], SimpleNamespace(sync_chunk_size=8)
        )
    assert [m.question for m in created] == [pm["question"], other["question"]]
    assert calls == [[pm["question"], other["question"]]]
    warnings, summary = _sync_lines(caplog)
    assert warnings == []
    assert summary.endswith("(0 failed)")


def test_a_market_state_error_names_its_reason_in_the_skip_line(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    db = fresh_test_db()
    bad = _pm(f"Unprepared {secrets.token_hex(4)}?")
    reason = "market not prepared on chain: outcome slots=0"

    monkeypatch.setattr(
        sync,
        "prepare_markets_on_chain",
        lambda admin, items: [MarketStateError(reason) for _ in items],
    )
    with db.write() as conn:
        sync.create_polymarket_markets_if_needed(
            conn, [bad], SimpleNamespace(sync_chunk_size=8)
        )
    warnings, _ = _sync_lines(caplog)
    assert len(warnings) == 1
    assert "MarketStateError" in warnings[0] and reason in warnings[0]
    assert "\n" not in warnings[0]
