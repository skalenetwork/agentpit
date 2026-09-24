"""Give every existing account a WorkOS identity, keeping its password.

WorkOS accepts a foreign bcrypt hash on user creation, so an imported account
signs in with the password it already had and nobody is asked to reset
anything. That is the whole reason this is a script and not an email to 17
people.

Idempotent by two mechanisms, because it will be re-run: a row that already
carries WORKOS_USER_ID is skipped outright, and `create_user` returns the
existing WorkOS user for an address rather than minting a second one. A failure
on one account is logged and the run continues -- one unusable address must not
cost the others their identity -- and each account's create+link is its own
savepoint so a failure cannot take the rest of the pass down with it.

Known non-fatal outcome: `users.EMAIL` is UNIQUE but case-SENSITIVE, so two
rows may differ only in case while WorkOS hands both the same `user_...` id.
The second one fails on `idx_users_workos_user_id` and is reported under
`failed=`. That is deliberate: which of the two duplicate accounts owns the
identity is a human's call, not this script's.

    .venv/bin/python -m scripts.migrate_users_to_workos --dry-run
    .venv/bin/python -m scripts.migrate_users_to_workos
"""
import argparse
import logging
import sys
from dataclasses import dataclass

from agentpit.auth.workos_client import WorkOsClient, build_workos_client
from agentpit.config import Settings
from agentpit.db.session import DbSession
from agentpit.db.table_write import TableWrite

log = logging.getLogger("migrate_users_to_workos")


@dataclass
class MigrationReport:
    migrated: int = 0
    skipped: int = 0
    failed: int = 0


def migrate_users(
    conn, client: WorkOsClient, *, dry_run: bool = False
) -> MigrationReport:
    report = MigrationReport()
    rows = conn.execute(
        "SELECT USER_ID, EMAIL, PASSWORD_HASH, WORKOS_USER_ID FROM users "
        "WHERE AGENT_APP IS NULL ORDER BY CREATED_AT"
    ).fetchall()
    for row in rows:
        user_id, email, password_hash, existing = (
            row["USER_ID"], row["EMAIL"], row["PASSWORD_HASH"], row["WORKOS_USER_ID"]
        )
        if existing:
            report.skipped += 1
            continue
        if dry_run:
            log.info("would migrate %s (%s)", email, user_id)
            report.migrated += 1
            continue
        try:
            # The savepoint is what makes "log it and carry on" true for a
            # DATABASE error, not just an HTTP one. Postgres aborts the entire
            # transaction on any failed statement, so without this the `except`
            # below catches the exception and then every later row dies with
            # InFailedSqlTransaction -- and psycopg turns the final COMMIT of an
            # aborted transaction into a silent ROLLBACK, so the report would
            # claim migrations that were thrown away. Measured against the test
            # database: two case-variant addresses (users.EMAIL is UNIQUE but
            # case-SENSITIVE, while WorkOS returns one `user_...` id for both)
            # collide on idx_users_workos_user_id, and that one collision left
            # all three rows unlinked while reporting migrated=1.
            with conn.transaction():
                workos_user = client.create_user(
                    email=email, password_hash=password_hash
                )
                if not TableWrite.set_workos_user_id(
                    conn, user_id, workos_user.workos_user_id
                ):
                    raise RuntimeError(f"no row to link for {user_id}")
        except Exception:
            log.exception("migrating %s failed", email)
            report.failed += 1
            continue
        log.info("migrated %s -> %s", email, workos_user.workos_user_id)
        report.migrated += 1
    return report


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = Settings()  # type: ignore[call-arg]
    client = build_workos_client(settings)
    if client is None:
        log.error("WORKOS_API_KEY / WORKOS_CLIENT_ID are not set")
        return 1

    # create_tables=False: this script is a second writer against a live API,
    # and running the schema creation from here has deadlocked production
    # before (see scripts/backfill_trade_match_kind.py for the same guard).
    db = DbSession(settings.database_url, create_tables=False)
    try:
        with db.write() as conn:
            report = migrate_users(conn, client, dry_run=args.dry_run)
    finally:
        db.close()
    log.info(
        "migrated=%d skipped=%d failed=%d%s",
        report.migrated, report.skipped, report.failed,
        " (dry run)" if args.dry_run else "",
    )
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
