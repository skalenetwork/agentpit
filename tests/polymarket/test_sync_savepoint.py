"""One bad market must not poison the whole sync batch.

Regression: the batch runs inside a single db.write() transaction
(app._run_polymarket_sync). A UniqueViolation on one market (e.g. two
Polymarket markets with the same question text derive the same local
condition id) used to abort the shared transaction — every later insert
died with InFailedSqlTransaction and the closing COMMIT silently became a
ROLLBACK, so a fresh database ended up with ZERO markets while the log
claimed success. First seen on the 2026-07-16 prod deploy.
"""

import secrets

import agentpit.polymarket.polymarket_sync as sync
from agentpit.datastructures.condition_id import ConditionId
from agentpit.db.table_read import TableRead
from tests.chain_fakes import as_batch
from tests.db_helpers import fresh_test_db


def _pm_market(question: str) -> dict:
    return {
        "id": int(secrets.token_hex(4), 16),
        "conditionId": "0x" + secrets.token_hex(32),
        "question": question,
        "description": "d",
        "slug": f"savepoint-{secrets.token_hex(4)}",
        "startDate": "2020-01-01T00:00:00Z",
        "endDate": "2020-01-02T00:00:00Z",
        "active": True,
        "closed": False,
        "tokens": [
            {"token_id": str(int(secrets.token_hex(8), 16)), "outcome": "Yes"},
            {"token_id": str(int(secrets.token_hex(8), 16)), "outcome": "No"},
        ],
    }


def test_duplicate_market_does_not_poison_batch(monkeypatch):
    db = fresh_test_db()
    good_a = _pm_market(f"Savepoint A {secrets.token_hex(4)}?")
    dupe = _pm_market(f"Savepoint dupe {secrets.token_hex(4)}?")
    dupe_again = _pm_market(dupe["question"])  # same question, different pm id
    good_b = _pm_market(f"Savepoint B {secrets.token_hex(4)}?")

    # Deterministic per-question condition ids: identical questions collide on
    # markets.CONDITION_ID exactly like the real keccak-derived ids do.
    def stable_prepare(admin, question, labels):
        cid = ConditionId("0x" + question.encode().hex()[:64].ljust(64, "0"))
        toks = [
            (str(int(secrets.token_hex(8), 16)), labels[0]),
            (str(int(secrets.token_hex(8), 16)), labels[1]),
        ]
        return cid, toks

    monkeypatch.setattr(sync, "prepare_markets_on_chain", as_batch(stable_prepare))

    with db.write() as conn:
        created = sync.create_polymarket_markets_if_needed(
            conn, [good_a, dupe, dupe_again, good_b], admin=None
        )

    # The duplicate is skipped; everything else must survive the commit.
    assert [m.question for m in created] == [
        good_a["question"],
        dupe["question"],
        good_b["question"],
    ]
    with db.read() as conn:
        rows = TableRead.list_markets_filtered(conn, limit=100)
    questions = {m.question for m in rows}
    assert good_a["question"] in questions
    assert dupe["question"] in questions
    assert good_b["question"] in questions
