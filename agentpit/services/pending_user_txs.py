"""User transactions whose outcome nobody saw.

A split, merge or claim can stop waiting without an answer (no receipt in
time, no answer to the broadcast, a failed receipt poll) and mine all the
same. Written only on success, its history row would then be missing for good:
the history loses it, auto-redeem misses a holder whose only stake is that
split, and a client retrying what it was told had failed splits twice.

So `PositionService` writes an intent row in `pending_user_txs` just before
each broadcast and turns it into the history row once the receipt is in; a
refusal or a revert deletes it. An unknown outcome keeps it: the caller gets
`TransactionPendingError` (503), and a new action on that market is a 409
while the row is younger than `_PENDING_TTL_SECONDS`.
`reconcile_pending_user_txs` settles the rest from the chain, at the start of
every auto-redeem pass (or on its own when auto-redeem is off).
"""

import logging
import time

from agentpit.db.session import DbSession
from agentpit.db.table_read import PendingUserTx, TableRead
from agentpit.db.table_write import TableWrite
from agentpit.onchain.admin import OnchainAdmin

logger = logging.getLogger(__name__)

# How long a pending row counts as a transaction still on its way. Far beyond
# the receipt timeout (30 s) and the auto-redeem cadence (a pass every 20 s on
# the pin loop), so a transaction that mines late is settled long before. A
# row this old with no receipt is a transaction the node lost or never took:
# the reconciler drops it, and the account's market is free again.
_PENDING_TTL_SECONDS = 600


def in_flight_since(now: int) -> int:
    """The CREATED_AT from which a pending row still counts as in flight, at
    unix time `now`: what the duplicate guard and the auto-redeem pass skip."""
    return now - _PENDING_TTL_SECONDS


def reconcile_pending_user_txs(db: DbSession, admin: OnchainAdmin) -> int:
    """Settle every pending user transaction from its receipt, the way the
    sending request would have; returns how many history rows it wrote.

    Mined: the row becomes its history row (a claim's amount is what the CTF
    paid its sender). Reverted, a claim that paid nothing, or no receipt past
    `_PENDING_TTL_SECONDS`: dropped with no history row. No receipt yet: left
    for the next pass. `TableWrite.confirm_pending_user_tx` lets exactly one
    of this and the sending request write the row. An error on one row is
    logged and that row is tried again next pass.
    """
    with db.read() as conn:
        rows = TableRead.list_pending_user_txs(conn)
    now = int(time.time())
    written = 0
    for row in rows:
        try:
            written += _settle(db, admin, row, now)
        except Exception:
            logger.exception(
                "pending %s transaction %s on market %s could not be settled; "
                "tried again next pass",
                row.transaction_type,
                row.tx_hash,
                row.market_id,
            )
    return written


def _settle(db: DbSession, admin: OnchainAdmin, row: PendingUserTx, now: int) -> int:
    """`reconcile_pending_user_txs` for one row: 1 if it wrote the history
    row, else 0."""
    receipt = admin.transaction_receipt(row.tx_hash)
    if receipt is None:
        if row.created_at >= in_flight_since(now):
            return 0
        age = now - row.created_at
        return _drop(db, row, "has had no receipt for %d s; dropped as lost", age)
    if receipt["status"] != 1:
        return _drop(db, row, "mined and reverted; dropped")
    details = dict(row.details)
    if row.transaction_type == "REDEEM":
        # The claim's sender is the redeemer `PayoutRedemption` names.
        paid = admin.redeemed_payout(receipt, receipt["from"])
        if paid <= 0:
            # Its tokens had left: no claim was made, as for one seen at once.
            return _drop(
                db,
                row,
                "mined but paid the claimant nothing; dropped without a history row",
            )
        details["collateral_amount"] = paid
    with db.write() as conn:
        confirmed = TableWrite.confirm_pending_user_tx(conn, row.tx_hash, details)
    if confirmed:
        logger.info(
            "pending %s transaction %s on market %s mined; written to the "
            "history",
            row.transaction_type,
            row.tx_hash,
            row.market_id,
        )
    return int(confirmed)


def _drop(db: DbSession, row: PendingUserTx, why: str, *args: object) -> int:
    """Delete `row` with no history row, log `why`, and return 0."""
    with db.write() as conn:
        TableWrite.delete_pending_user_tx(conn, row.tx_hash)
    logger.warning(
        "pending %s transaction %s on market %s " + why,
        row.transaction_type,
        row.tx_hash,
        row.market_id,
        *args,
    )
    return 0
