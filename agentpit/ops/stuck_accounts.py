"""Accounts left half-deleted: a person's row whose WorkOS user is gone.

`DELETE /me` deletes the WorkOS user before it clears the row. A failure in
between leaves the row live under an identity WorkOS no longer has: its
address stored, its agents trading on their API keys, its owner name on the
boards. Deleting the user in the WorkOS dashboard, or from another deployment
on the same WorkOS environment, leaves exactly the same row. Nothing here can
tell those apart, so this only lists them, for a person to decide about.

`came_back` names the WorkOS users that signed in with the same address since
and were refused. Sign-in now hands a row like this to a new identity for its
verified address, so anyone who signs in again gets the account back on their
own; the rest stay as they are until somebody finishes the delete by hand.

Local-only for the reasons `owner_report` gives: it prints addresses to the
terminal and writes nothing. It reads the database in one READ ONLY
transaction, asks WorkOS about every account, and about the refused sign-ins
only when something is stuck.

    ssh <prod> docker exec agentpit-api python -m agentpit.ops.stuck_accounts
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
import time
from collections.abc import Callable, Iterable

from agentpit.auth.workos_client import (
    WorkOsClient,
    WorkOsError,
    WorkOsNotFoundError,
    WorkOsRateLimitedError,
    WorkOsUser,
    build_workos_client,
)
from agentpit.config import Settings
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead

COLUMNS = ["email", "user_id", "gone_workos_id", "created", "owner_name", "live_agents", "came_back"]

#: How often a rate-limited lookup is tried, and the wait in between. WorkOS
#: limits per minute, so a few waits get a list of hundreds through.
ATTEMPTS = 5
WAIT_SECONDS = 15.0


def lookup(
    client: WorkOsClient,
    workos_user_id: str,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> WorkOsUser | None:
    """The WorkOS user, or None when WorkOS answers 404 for it.

    A rate limit is waited out. Every other failure is raised: an outage or a
    refused key says nothing about whether the user is gone, and a list built
    on a guess would name accounts that are fine.
    """
    for _ in range(ATTEMPTS - 1):
        try:
            return _get(client, workos_user_id)
        except WorkOsRateLimitedError:
            sleep(WAIT_SECONDS)
    return _get(client, workos_user_id)


def _get(client: WorkOsClient, workos_user_id: str) -> WorkOsUser | None:
    try:
        return client.get_user(workos_user_id)
    except WorkOsNotFoundError:
        return None


def build_rows(
    people: Iterable[dict],
    unheld: Iterable[str],
    find: Callable[[str], WorkOsUser | None],
) -> list[list[str]]:
    """One line per person's row whose WorkOS user `find` does not find.

    The refused identities in `unheld` are matched to those rows by address,
    case-insensitively: WorkOS reports a normalised address, ours was stored
    as typed.
    """
    stuck = [p for p in people if find(p["WORKOS_USER_ID"]) is None]
    came_back: dict[str, list[str]] = {}
    if stuck:
        for workos_id in unheld:
            user = find(workos_id)
            if user is not None:
                came_back.setdefault(user.email.lower(), []).append(workos_id)
    out: list[list[str]] = []
    for p in stuck:
        email = p["EMAIL"] or ""
        created = p["CREATED_AT"]
        out.append([
            email,
            p["USER_ID"],
            p["WORKOS_USER_ID"],
            dt.datetime.fromtimestamp(created, dt.UTC).strftime("%Y-%m-%d") if created else "",
            p["OWNER_NAME"] or "",
            str(p["LIVE_AGENTS"]),
            ",".join(came_back.get(email.lower(), [])),
        ])
    return out


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.parse_args(argv)
    settings = Settings()
    client = build_workos_client(settings)
    if client is None:
        sys.exit("WorkOS is not configured, so no account can be checked")
    # As in `owner_report`: no DDL against the live database, one connection,
    # and a READ ONLY transaction so Postgres itself refuses any write.
    db = DbSession(settings.database_url, min_size=1, max_size=1, create_tables=False)
    try:
        with db.read() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            people = TableRead.workos_people(conn)
            unheld = TableRead.unheld_owners(conn)
    finally:
        db.close()
    try:
        lines = build_rows(people, unheld, lambda workos_id: lookup(client, workos_id))
    except WorkOsError as exc:
        # Nothing rather than part of the list: a row missing from it would
        # read as an account that is fine. The message names an id, never an
        # address.
        sys.exit(f"stopped before listing anything, WorkOS did not answer: {exc}")
    writer = csv.writer(sys.stdout, delimiter="\t", lineterminator="\n")
    writer.writerow(COLUMNS)
    writer.writerows(lines)
    print(f"{len(lines)} of {len(people)} accounts hold a WorkOS user that is gone", file=sys.stderr)
    if len(people) > 1 and len(lines) == len(people):
        # The one misconfiguration that lists every account: a key for another
        # WorkOS environment, where none of these ids exist.
        print(
            "WorkOS has none of them: check that WORKOS_API_KEY belongs to this deployment's environment",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
