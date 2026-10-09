"""User transactions whose outcome nobody saw.

A split, merge or claim is signed by the user's key, broadcast, and waited on
(`UserGasSponsor.send`). The wait can end without an answer: the receipt does
not come back in time (`TimeExhausted`), or the node never answers the
broadcast (a transport error), or the receipt poll fails after the node took
it. The transaction may mine all the same. Written only on success, its
SPLIT / MERGE / REDEEM row would then be missing for good: the history loses
it, auto-redeem's participant scan (trades plus SPLIT / MERGE rows) misses a
holder whose only stake is that split, and a client that retries a split it
was told had failed splits twice.

So `PositionService` writes an intent row in `pending_user_txs` just before
each broadcast, and turns it into the history row once the receipt is in. A
refusal or a revert deletes it. When the outcome stays unknown, the row stays:
after a receipt timeout, an unanswered broadcast, a receipt-poll error, and
any error not recognised as a refusal (safe, since an unrecognised refusal only
costs the account 409s on the market until the row is dropped for want of a
receipt). The caller gets `TransactionPendingError` (503), and a new split,
merge or claim on that market is refused (409) while the row is younger than
`_PENDING_TTL_SECONDS`. `reconcile_pending_user_txs` settles what is left from
the chain: at the start of every auto-redeem pass, and on its own in both
resolution loops when auto-redeem is switched off.
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
    """Settle every pending user transaction from its receipt. Returns how many
    history rows it wrote.

    - Mined (status 1): its SPLIT / MERGE / REDEEM row is written and the
      pending row deleted, in one statement. A claim's amount is what the
      CTF paid its sender (`OnchainAdmin.redeemed_payout`), as for a claim
      confirmed on the spot.
    - Reverted (status 0): deleted, and no row, as for a revert seen at once.
    - No receipt and older than `_PENDING_TTL_SECONDS`: deleted, as lost.
    - No receipt yet: left for the next pass.

    The request that sent a transaction may confirm it at the same moment;
    `TableWrite.confirm_pending_user_tx` lets exactly one of the two write the
    row. An error on one row is logged with its traceback and that row is
    tried again next pass; the others are settled all the same.
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
        with db.write() as conn:
            TableWrite.delete_pending_user_tx(conn, row.tx_hash)
        logger.warning(
            "pending %s transaction %s on market %s has had no receipt for "
            "%d s; dropped as lost",
            row.transaction_type,
            row.tx_hash,
            row.market_id,
            now - row.created_at,
        )
        return 0
    if receipt["status"] != 1:
        with db.write() as conn:
            TableWrite.delete_pending_user_tx(conn, row.tx_hash)
        logger.warning(
            "pending %s transaction %s on market %s mined and reverted; dropped",
            row.transaction_type,
            row.tx_hash,
            row.market_id,
        )
        return 0
    details = dict(row.details)
    if row.transaction_type == "REDEEM":
        # The claim's sender is the redeemer `PayoutRedemption` names.
        details["collateral_amount"] = admin.redeemed_payout(receipt, receipt["from"])
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
