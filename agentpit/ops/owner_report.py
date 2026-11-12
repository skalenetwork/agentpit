"""Who runs which agents: owner email, owner name, and each live agent.

Local-only, on purpose. There is no HTTP route for this and there must not be
one: an admin token is a bearer secret that leaks the way bearer secrets do,
and behind it this would hand out every user's email. The report runs inside
the API container over ssh, prints to the terminal, and writes nothing --
no table, no file, no log line carries an address.

Emails come from the owner's own web account where there is one. Owners who
only ever connected over MCP have no address stored here (their tokens carry
none), so for those it asks WorkOS at the moment the report runs and keeps
nothing afterwards. `--no-workos` skips that and leaves the column blank.

    ssh <prod> docker exec agentpit-api python -m agentpit.ops.owner_report
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
from collections.abc import Callable, Iterable

from agentpit.auth.workos_client import WorkOsError, WorkOsRateLimitedError, build_workos_client
from agentpit.config import Settings
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead

COLUMNS = ["owner_email", "owner_name", "owner_workos_id", "agent_handle", "agent_app", "agent_client", "agent_address", "agent_created"]


def build_rows(rows: Iterable[dict], lookup: Callable[[str], str | None] | None) -> list[list[str]]:
    """One output line per agent (one per agentless owner). `lookup` resolves
    an owner with no local email, at most once per owner; None skips it."""
    resolved: dict[str, str | None] = {}
    out: list[list[str]] = []
    for r in rows:
        owner = r["WORKOS_ID"]
        if owner not in resolved:
            resolved[owner] = r["WEB_EMAIL"] or (lookup(owner) if lookup else None)
        created = r["AGENT_CREATED_AT"]
        out.append([
            resolved[owner] or "",
            r["NAME"],
            owner,
            r["AGENT_HANDLE"] or "",
            r["AGENT_APP"] or "",
            r["AGENT_CLIENT"] or "",
            r["AGENT_ADDRESS"] or "",
            dt.datetime.fromtimestamp(created, dt.UTC).strftime("%Y-%m-%d") if created else "",
        ])
    out.sort(key=lambda line: (line[0].lower() or "~", line[2], line[7]))
    return out


def _workos_lookup(settings: Settings) -> Callable[[str], str | None] | None:
    client = build_workos_client(settings)
    if client is None:
        print("WorkOS is not configured; MCP-only owners show no email", file=sys.stderr)
        return None
    stopped = False

    def lookup(workos_id: str) -> str | None:
        nonlocal stopped
        if stopped:
            return None
        try:
            return client.get_user(workos_id).email
        except WorkOsRateLimitedError:
            stopped = True
            print("WorkOS rate limit hit; remaining owners show no email, rerun later", file=sys.stderr)
        except WorkOsError:
            # The id only: never an address, even on stderr.
            print(f"WorkOS has no user {workos_id}", file=sys.stderr)
        return None

    return lookup


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--no-workos", action="store_true", help="use only emails stored locally")
    args = parser.parse_args(argv)
    settings = Settings()
    db = DbSession(settings.database_url)
    try:
        with db.read() as conn:
            rows = TableRead.owner_agent_rows(conn)
    finally:
        db.close()
    lines = build_rows(rows, None if args.no_workos else _workos_lookup(settings))
    writer = csv.writer(sys.stdout, delimiter="\t", lineterminator="\n")
    writer.writerow(COLUMNS)
    writer.writerows(lines)


if __name__ == "__main__":
    main()
