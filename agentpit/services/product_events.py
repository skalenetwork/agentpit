"""Product events: milestones in a person's and an agent's life, for growth
analytics.

`track` writes a row to `product_events` inside the caller's transaction, so
an event exists exactly when the action it records committed. A forwarder
sends unsent rows to PostHog in batches, so a vendor outage never slows or
fails a trade, and changing vendor is a change to the forwarder alone.

What an event may carry: event names, agent addresses (already public),
runner slugs, competition slugs, market slugs, counts and amounts. Never an
email, an API key, a token, an IP address or free text a person typed. The
owner's WorkOS id stays in our database; PostHog sees only an HMAC of it.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Any

import httpx
import psycopg

from agentpit.db.session import DbSession
from agentpit.domain.runner import runner_for

log = logging.getLogger(__name__)

# Each event's PostHog uuid comes from its row id, so a batch sent again after
# a failure is deduplicated by PostHog instead of counted twice.
_UUIDS = uuid.UUID("5b0f8c55-3c5e-4f57-9a43-0d1f1c6a7e21")

# Sent rows older than this are batched as a historical import, so PostHog
# files them under their own day instead of treating them as live traffic.
HISTORICAL_AFTER_S = 86_400
BATCH = 500


def track(
    conn: psycopg.Connection,
    name: str,
    *,
    owner: str | None = None,
    agent: str | None = None,
    at: int | None = None,
    **props: Any,
) -> None:
    """Record one event in the caller's transaction. A once-in-a-lifetime
    event that already exists is skipped. A failure is logged and swallowed
    inside a savepoint: analytics never fails the action it records."""
    try:
        with conn.transaction():
            conn.execute(
                "INSERT INTO product_events (AT, NAME, OWNER_WORKOS_ID, AGENT_ADDRESS, PROPS) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (
                    int(time.time()) if at is None else at,
                    name,
                    owner,
                    agent.lower() if agent else None,
                    json.dumps({k: v for k, v in props.items() if v is not None}),
                ),
            )
    except Exception:
        log.exception("could not record product event %s", name)


def owner_created(conn: psycopg.Connection, workos_id: str, via: str) -> bool:
    """Create the owner row when there is none, and record the sign-up when
    this call created it. True when it did."""
    from agentpit.db.table_write import TableWrite

    created = TableWrite.ensure_owner(conn, workos_id, via)
    if created:
        track(conn, "owner_signed_up", owner=workos_id, via=via)
    return created


def first_trade(conn: psycopg.Connection, api_key: str, market: str, at: int) -> None:
    """The fill's taker made its first trade, when it is a person's agent
    that has none recorded yet. One statement: the unique index turns every
    later fill into a no-op, and house and bot accounts match no row."""
    try:
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO product_events (AT, NAME, OWNER_WORKOS_ID, AGENT_ADDRESS, PROPS)
                SELECT %s, 'first_trade', u.OWNER_WORKOS_ID, LOWER(u.ETH_ADDRESS),
                       jsonb_build_object('market', %s::text, 'agent_age_s', %s - u.CREATED_AT)
                FROM users u
                WHERE u.API_KEY = %s AND u.OWNER_WORKOS_ID IS NOT NULL AND u.IS_BOT = 0 AND u.DELETED_AT IS NULL
                ON CONFLICT DO NOTHING
                """,
                (at, market, at, api_key),
            )
    except Exception:
        log.exception("could not record a first trade")


# A person's agent that may carry events: not a bot, not deleted, and its
# owner row still exists (account deletion removes it).
_LIVE_AGENT = (
    "u.OWNER_WORKOS_ID IS NOT NULL AND u.IS_BOT = 0 AND u.DELETED_AT IS NULL AND u.ETH_ADDRESS IS NOT NULL "
    "AND EXISTS (SELECT 1 FROM owners o WHERE o.WORKOS_ID = u.OWNER_WORKOS_ID)"
)


def backfill(conn: psycopg.Connection) -> None:
    """The milestones that happened before events were recorded, from the
    tables that hold them, at their original times. Idempotent: run at every
    startup, it adds only what is missing, and first-trade detection depends
    on it marking every agent that traded before this code existed. Deleted
    accounts and their agents are left out."""
    conn.execute(
        """
        INSERT INTO product_events (AT, NAME, OWNER_WORKOS_ID, PROPS)
        SELECT COALESCE(LEAST(o.CREATED_AT, MIN(u.CREATED_AT)), EXTRACT(EPOCH FROM now())::bigint),
               'owner_signed_up', o.WORKOS_ID,
               jsonb_build_object('via', COALESCE(o.SIGNUP_VIA, 'unknown'), 'backfill', true)
        FROM owners o
        JOIN users u ON (u.OWNER_WORKOS_ID = o.WORKOS_ID OR u.WORKOS_USER_ID = o.WORKOS_ID) AND u.DELETED_AT IS NULL
        WHERE NOT EXISTS (
            SELECT 1 FROM product_events e WHERE e.NAME = 'owner_signed_up' AND e.OWNER_WORKOS_ID = o.WORKOS_ID
        )
        GROUP BY o.WORKOS_ID, o.CREATED_AT, o.SIGNUP_VIA
        ON CONFLICT DO NOTHING
        """
    )
    # agent_created carries the runner slug, as the live event does, never
    # the app name an MCP client chose for itself.
    created = conn.execute(
        f"""
        SELECT u.CREATED_AT, u.OWNER_WORKOS_ID, LOWER(u.ETH_ADDRESS) AS ADDRESS, u.AGENT_APP, u.AGENT_HOST
        FROM users u
        WHERE {_LIVE_AGENT} AND NOT EXISTS (
            SELECT 1 FROM product_events e WHERE e.NAME = 'agent_created' AND e.AGENT_ADDRESS = LOWER(u.ETH_ADDRESS)
        )
        """
    ).fetchall()
    for row in created:
        conn.execute(
            "INSERT INTO product_events (AT, NAME, OWNER_WORKOS_ID, AGENT_ADDRESS, PROPS) "
            "VALUES (%s, 'agent_created', %s, %s, %s) ON CONFLICT DO NOTHING",
            (
                row["CREATED_AT"],
                row["OWNER_WORKOS_ID"],
                row["ADDRESS"],
                json.dumps(
                    {
                        "runner": runner_for(row["AGENT_APP"], row["AGENT_HOST"]).slug,
                        "source": "mcp" if row["AGENT_APP"] else "api_key",
                        "backfill": True,
                    }
                ),
            ),
        )
    conn.execute(
        f"""
        INSERT INTO product_events (AT, NAME, OWNER_WORKOS_ID, AGENT_ADDRESS, PROPS)
        SELECT f.MATCH_TIME, 'first_trade', u.OWNER_WORKOS_ID, LOWER(u.ETH_ADDRESS),
               jsonb_build_object('market', f.MARKET, 'agent_age_s', f.MATCH_TIME - u.CREATED_AT, 'backfill', true)
        FROM users u
        CROSS JOIN LATERAL (
            SELECT t.MATCH_TIME, t.MARKET FROM trades t
            WHERE t.TAKER_API_KEY = u.API_KEY AND t.STATUS <> 'FAILED'
            ORDER BY t.MATCH_TIME LIMIT 1
        ) f
        WHERE {_LIVE_AGENT} AND NOT EXISTS (
            SELECT 1 FROM product_events e WHERE e.NAME = 'first_trade' AND e.AGENT_ADDRESS = LOWER(u.ETH_ADDRESS)
        )
        ON CONFLICT DO NOTHING
        """
    )
    # An agent may enter, leave and enter again, so an entry is matched on its
    # time as well, not just on the agent and the competition.
    conn.execute(
        f"""
        INSERT INTO product_events (AT, NAME, OWNER_WORKOS_ID, AGENT_ADDRESS, PROPS)
        SELECT ce.ENROLLED_AT, 'competition_entered', ce.OWNER, LOWER(u.ETH_ADDRESS),
               jsonb_build_object('competition', ce.SLUG, 'backfill', true)
        FROM competition_entries ce JOIN users u ON u.USER_ID = ce.USER_ID
        WHERE ce.ENROLLED_AT IS NOT NULL AND ce.OWNER = u.OWNER_WORKOS_ID AND {_LIVE_AGENT}
          AND NOT EXISTS (
            SELECT 1 FROM product_events e
            WHERE e.NAME = 'competition_entered' AND e.AGENT_ADDRESS = LOWER(u.ETH_ADDRESS)
              AND e.PROPS->>'competition' = ce.SLUG AND e.AT = ce.ENROLLED_AT
          )
        """
    )


def owner_hash(secret: str, workos_id: str) -> str:
    return hmac.new(secret.encode(), workos_id.encode(), hashlib.sha256).hexdigest()[:32]


class Forwarder:
    """Sends unsent product events to PostHog's batch endpoint and marks them
    sent. A failed send leaves them unsent for the next round."""

    def __init__(
        self,
        db: DbSession,
        api_key: str,
        host: str,
        id_secret: str,
        env: str,
        interval_s: float = 15.0,
        client: httpx.Client | None = None,
    ):
        self._db = db
        self._api_key = api_key
        self._url = host.rstrip("/") + "/batch/"
        self._secret = id_secret
        self._env = env
        self._interval = interval_s
        self._client = client or httpx.Client(timeout=20.0)

    async def run(self) -> None:
        """Flush every interval. While sends fail, wait twice as long each
        time, up to 15 minutes, and log the failure once with its traceback
        and then one line per retry."""
        failures = 0
        while True:
            try:
                while await asyncio.to_thread(self.flush) == BATCH:
                    pass
                if failures:
                    log.info("product events: forwarding recovered after %d failed attempts", failures)
                failures = 0
            except Exception as exc:
                if failures == 0:
                    log.exception("product events: forwarding failed; will retry with backoff")
                else:
                    log.warning("product events: forwarding still failing (%s)", type(exc).__name__)
                failures += 1
            await asyncio.sleep(min(self._interval * 2 ** min(failures, 10), 900.0))

    def payload(self, row: dict) -> dict:
        owner = row["OWNER_WORKOS_ID"]
        agent = row["AGENT_ADDRESS"]
        props: dict[str, Any] = {
            **(row["PROPS"] or {}),
            "$process_person_profile": False,
            "$lib": "agentpit-server",
            "env": self._env,
        }
        if agent:
            props["agent_address"] = agent
        if owner:
            props["owner_hash"] = owner_hash(self._secret, owner)
            for key, column in (
                ("signup_via", "SIGNUP_VIA"),
                ("first_ref", "FIRST_REF"),
                ("first_utm_source", "UTM_SOURCE"),
                ("first_utm_medium", "UTM_MEDIUM"),
                ("first_utm_campaign", "UTM_CAMPAIGN"),
            ):
                if row[column]:
                    props[key] = row[column]
        return {
            "event": row["NAME"],
            "distinct_id": props.get("owner_hash") or agent or "anonymous",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(row["AT"])),
            "uuid": str(uuid.uuid5(_UUIDS, str(row["ID"]))),
            "properties": props,
        }

    def flush(self) -> int:
        """Send one batch of the oldest unsent events. The number it held.
        The rows are read and marked in two short transactions, never held
        open across the HTTP call."""
        now = int(time.time())
        with self._db.read() as conn:
            rows = conn.execute(
                """
                SELECT e.ID, e.AT, e.NAME, e.OWNER_WORKOS_ID, e.AGENT_ADDRESS, e.PROPS,
                       o.SIGNUP_VIA, o.FIRST_REF, o.UTM_SOURCE, o.UTM_MEDIUM, o.UTM_CAMPAIGN
                FROM product_events e LEFT JOIN owners o ON o.WORKOS_ID = e.OWNER_WORKOS_ID
                WHERE e.SENT_AT IS NULL
                ORDER BY e.ID LIMIT %s
                """,
                (BATCH,),
            ).fetchall()
        if not rows:
            return 0
        for historical in (False, True):
            part = [r for r in rows if (now - r["AT"] > HISTORICAL_AFTER_S) == historical]
            if not part:
                continue
            body: dict[str, Any] = {"api_key": self._api_key, "batch": [self.payload(r) for r in part]}
            if historical:
                body["historical_migration"] = True
            self._client.post(self._url, json=body).raise_for_status()
        with self._db.write() as conn:
            conn.execute("UPDATE product_events SET SENT_AT = %s WHERE ID = ANY(%s)", (now, [r["ID"] for r in rows]))
        return len(rows)
