"""The sync prepares new markets on chain in chunks, after classifying every
candidate, and one market's chain failure fails only that market."""

import secrets
from types import SimpleNamespace

import agentpit.polymarket.polymarket_sync as sync
from agentpit.datastructures.condition_id import ConditionId
from tests.db_helpers import fresh_test_db


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


def test_a_chunk_that_raises_fails_only_its_own_markets(monkeypatch):
    db = fresh_test_db()
    markets = [_pm(f"Raise {i} {secrets.token_hex(4)}?") for i in range(4)]
    seen = {"n": 0}

    def fake_batch(admin, items):
        seen["n"] += 1
        if seen["n"] == 1:
            raise ConnectionError("RPC down")
        return [_ids(labels) for _, labels in items]

    monkeypatch.setattr(sync, "prepare_markets_on_chain", fake_batch)
    with db.write() as conn:
        created = sync.create_polymarket_markets_if_needed(
            conn, markets, SimpleNamespace(sync_chunk_size=2)
        )
    assert [m.question for m in created] == [pm["question"] for pm in markets[2:]]
