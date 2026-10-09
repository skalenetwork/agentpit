"""`reconcile_pending_user_txs` settles the user transactions whose outcome
nobody saw.

A split, merge or claim whose receipt did not come back in time (or whose
broadcast got no answer) leaves its intent row in `pending_user_txs`. The
auto-redeem pass calls the reconciler first, and it reads each row's receipt
by hash: a mined transaction gets its history row, a reverted or long-lost one
is dropped, and one still on its way is left for the next pass. The chain is a
fake here; tests/onchain/test_pending_user_txs.py runs the same against anvil.
"""

from __future__ import annotations

import json
import logging
import time

from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.services.pending_user_txs import (
    _PENDING_TTL_SECONDS,
    reconcile_pending_user_txs,
)
from tests.db_helpers import fresh_test_db

_LOGGER = "agentpit.services.pending_user_txs"
_REDEEMER = "0x00000000000000000000000000000000000000Aa"


def _hash(n: int) -> str:
    return "0x%064x" % n


class _Chain:
    """The two things the reconciler asks of `OnchainAdmin`.

    `receipts[tx_hash]` is the receipt `transaction_receipt` answers for that
    hash: a dict, None (the chain has none), or an exception it raises.
    `redeemed_payout` reads a receipt's `payout` and remembers whose it was
    asked for."""

    def __init__(self, receipts: dict):
        self.receipts = receipts
        self.read: list[str] = []
        self.payout_reads: list[tuple[dict, str]] = []

    def transaction_receipt(self, tx_hash: str):
        self.read.append(tx_hash)
        answer = self.receipts.get(tx_hash)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def redeemed_payout(self, receipt, redeemer: str) -> int:
        self.payout_reads.append((receipt, redeemer))
        return receipt["payout"]


def _pend(db, n: int, kind: str, details: dict, *, age: int = 60, market_id: int = 7):
    with db.write() as conn:
        TableWrite.insert_pending_user_tx(
            conn, _hash(n), "k1", kind, market_id, details,
            created_at=int(time.time()) - age,
        )


def _pending_hashes(db) -> list[str]:
    with db.read() as conn:
        return [r.tx_hash for r in TableRead.list_pending_user_txs(conn)]


def _history(db) -> list[tuple[str, int, dict]]:
    with db.read() as conn:
        return [
            (r["TRANSACTION_TYPE"], r["MARKET_ID"], json.loads(r["DETAILS"]))
            for r in conn.execute(
                "SELECT TRANSACTION_TYPE, MARKET_ID, DETAILS FROM transactions "
                "WHERE API_KEY = 'k1' ORDER BY TRANSACTION_ID"
            ).fetchall()
        ]


def test_a_mined_transaction_becomes_its_history_row():
    """A claim's amount is what its receipt says the CTF paid the sender, as
    for a claim confirmed on the spot."""
    db = fresh_test_db()
    _pend(db, 1, "SPLIT", {"amount": 40_000_000})
    _pend(db, 2, "MERGE", {"amount": 15_000_000})
    _pend(db, 3, "REDEEM", {})
    claim = {"status": 1, "from": _REDEEMER, "payout": 100_000_000}
    chain = _Chain(
        {
            _hash(1): {"status": 1, "from": _REDEEMER},
            _hash(2): {"status": 1, "from": _REDEEMER},
            _hash(3): claim,
        }
    )

    assert reconcile_pending_user_txs(db, chain) == 3  # type: ignore[arg-type]

    assert _history(db) == [
        ("SPLIT", 7, {"amount": 40_000_000}),
        ("MERGE", 7, {"amount": 15_000_000}),
        ("REDEEM", 7, {"collateral_amount": 100_000_000}),
    ]
    assert chain.payout_reads == [(claim, _REDEEMER)]
    assert _pending_hashes(db) == []


def test_a_claim_that_mined_with_no_payout_is_dropped_without_a_row(caplog):
    """A claim whose request lost its answer and which then mined at a payout
    of zero (its tokens had left, say) is no claim: the same rule as a claim
    confirmed on the spot, which writes no REDEEM row at zero. The row would
    read as a lost market on the profile page."""
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    db = fresh_test_db()
    _pend(db, 1, "REDEEM", {})
    _pend(db, 2, "SPLIT", {"amount": 40_000_000})
    chain = _Chain(
        {
            _hash(1): {"status": 1, "from": _REDEEMER, "payout": 0},
            _hash(2): {"status": 1, "from": _REDEEMER},
        }
    )

    assert reconcile_pending_user_txs(db, chain) == 1  # type: ignore[arg-type]

    assert _history(db) == [("SPLIT", 7, {"amount": 40_000_000})]
    assert _pending_hashes(db) == []
    warnings = [
        r for r in caplog.records if r.name == _LOGGER and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1 and _hash(1) in warnings[0].getMessage()


def test_a_reverted_transaction_is_dropped_without_a_row():
    db = fresh_test_db()
    _pend(db, 1, "REDEEM", {})
    chain = _Chain({_hash(1): {"status": 0, "from": _REDEEMER}})

    assert reconcile_pending_user_txs(db, chain) == 0  # type: ignore[arg-type]

    assert _pending_hashes(db) == []
    assert _history(db) == []


def test_a_transaction_with_no_receipt_is_dropped_after_the_ttl():
    """Ten minutes with no receipt: the node lost it, or never had it."""
    db = fresh_test_db()
    _pend(db, 1, "SPLIT", {"amount": 1}, age=_PENDING_TTL_SECONDS + 1)
    chain = _Chain({_hash(1): None})

    assert reconcile_pending_user_txs(db, chain) == 0  # type: ignore[arg-type]

    assert _pending_hashes(db) == []
    assert _history(db) == []


def test_a_transaction_with_no_receipt_yet_is_left_for_the_next_pass():
    db = fresh_test_db()
    _pend(db, 1, "SPLIT", {"amount": 1}, age=_PENDING_TTL_SECONDS - 30)
    chain = _Chain({_hash(1): None})

    assert reconcile_pending_user_txs(db, chain) == 0  # type: ignore[arg-type]

    assert _pending_hashes(db) == [_hash(1)]
    assert _history(db) == []


def test_an_error_on_one_row_is_logged_and_the_rest_are_settled(caplog):
    db = fresh_test_db()
    _pend(db, 1, "SPLIT", {"amount": 1}, age=90)
    _pend(db, 2, "SPLIT", {"amount": 2}, age=60)
    _pend(db, 3, "MERGE", {"amount": 3}, age=30)
    chain = _Chain(
        {
            _hash(1): {"status": 1, "from": _REDEEMER},
            _hash(2): ConnectionError("connection reset"),
            _hash(3): {"status": 1, "from": _REDEEMER},
        }
    )

    assert reconcile_pending_user_txs(db, chain) == 2  # type: ignore[arg-type]

    assert chain.read == [_hash(1), _hash(2), _hash(3)]
    assert _history(db) == [("SPLIT", 7, {"amount": 1}), ("MERGE", 7, {"amount": 3})]
    assert _pending_hashes(db) == [_hash(2)]  # tried again next pass
    errors = [r for r in caplog.records if r.name == _LOGGER and r.levelno >= logging.ERROR]
    assert len(errors) == 1
    assert _hash(2) in errors[0].getMessage()
    assert errors[0].exc_info is not None


def test_nothing_pending_reads_nothing():
    db = fresh_test_db()
    chain = _Chain({})
    assert reconcile_pending_user_txs(db, chain) == 0  # type: ignore[arg-type]
    assert chain.read == []
