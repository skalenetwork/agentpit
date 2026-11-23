"""Product events: milestones in a person's and an agent's life, for growth
analytics.

Sign-ups, agents, first trades and active days are derived from the tables
that hold them, so they cannot disagree with their source and add nothing to
the request path. Competition entries and exits and top-ups leave no history
of their own, so `track` records them in the caller's transaction.

A forwarder sends unsent rows to PostHog and erases a person's data there when
they delete their account. A vendor outage never slows or fails a trade.

What an event may carry: event names, agent addresses (already public), runner
slugs, competition slugs, market slugs, counts and amounts. Never an email, an
API key, a token, an IP address or free text a person typed. The owner's WorkOS
id stays in our database; PostHog sees only an HMAC of it.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import psycopg

from agentpit.db.session import DbSession
from agentpit.domain.runner import runner_for

log = logging.getLogger(__name__)

Row = dict[str, Any]
Conn = psycopg.Connection[Any]

CAPTURE_URL = "https://eu.i.posthog.com/i/v1/analytics/events"
ERASE_URL = "https://eu.posthog.com/api/projects/{project_id}/persons/bulk_delete/"

DAY = 86_400
BATCH = 500
ERASE_BATCH = 100
ERASE_AFTER_S = 300
HISTORICAL_AFTER_S = DAY
MAX_BACKOFF_S = 900.0
DELIVERED = frozenset({"ok", "warning", "drop"})

_UUIDS = uuid.UUID("5b0f8c55-3c5e-4f57-9a43-0d1f1c6a7e21")
_INSERT = "INSERT INTO product_events (AT, NAME, OWNER_WORKOS_ID, AGENT_ADDRESS, PROPS)"
_ACCOUNT_OWNER = "COALESCE(u.OWNER_WORKOS_ID, u.WORKOS_USER_ID)"
_PERSON_PROPS = (
    ("signup_via", "SIGNUP_VIA"),
    ("first_ref", "FIRST_REF"),
    ("first_utm_source", "UTM_SOURCE"),
    ("first_utm_medium", "UTM_MEDIUM"),
    ("first_utm_campaign", "UTM_CAMPAIGN"),
)


def track(
    conn: Conn,
    name: str,
    *,
    owner: str,
    agent: str | None = None,
    at: int | None = None,
    **props: Any,
) -> None:
    """Record one event in the caller's transaction. A failure is logged and
    swallowed inside a savepoint: analytics never fails the action it records."""
    try:
        with conn.transaction():
            conn.execute(
                f"{_INSERT} VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
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


def top_up(conn: Conn, user_id: str, at: int, usd: int) -> None:
    """Record a top-up of an account whose owner still exists."""
    row = conn.execute(
        f"SELECT {_ACCOUNT_OWNER} AS OWNER, u.ETH_ADDRESS FROM users u "
        f"JOIN owners o ON o.WORKOS_ID = {_ACCOUNT_OWNER} WHERE u.USER_ID = %s AND u.IS_BOT = 0",
        (user_id,),
    ).fetchone()
    if row is not None:
        track(conn, "top_up", owner=row["OWNER"], agent=row["ETH_ADDRESS"], at=at, usd=usd)


def derive(conn: Conn) -> None:
    """Add every milestone the source tables hold and `product_events` does
    not, at its original time. Idempotent. A deleted account has no owner row,
    so its history never comes back."""
    conn.execute(
        f"""
        {_INSERT}
        SELECT COALESCE(LEAST(o.CREATED_AT, MIN(u.CREATED_AT)), EXTRACT(EPOCH FROM now())::bigint),
               'owner_signed_up', o.WORKOS_ID, NULL,
               jsonb_build_object('via', COALESCE(o.SIGNUP_VIA, 'unknown'))
        FROM owners o
        JOIN users u ON o.WORKOS_ID IN (u.OWNER_WORKOS_ID, u.WORKOS_USER_ID) AND u.IS_BOT = 0
        WHERE NOT EXISTS (
            SELECT 1 FROM product_events e WHERE e.NAME = 'owner_signed_up' AND e.OWNER_WORKOS_ID = o.WORKOS_ID
        )
        GROUP BY o.WORKOS_ID, o.CREATED_AT, o.SIGNUP_VIA
        ON CONFLICT DO NOTHING
        """
    )
    agents = conn.execute(
        """
        SELECT u.CREATED_AT, u.OWNER_WORKOS_ID, LOWER(u.ETH_ADDRESS) AS ADDRESS, u.AGENT_APP, u.AGENT_HOST,
               (SELECT COUNT(*) FROM users p
                WHERE p.OWNER_WORKOS_ID = u.OWNER_WORKOS_ID
                  AND (p.CREATED_AT, p.USER_ID) < (u.CREATED_AT, u.USER_ID)) AS AGENTS_BEFORE
        FROM users u
        JOIN owners o ON o.WORKOS_ID = u.OWNER_WORKOS_ID
        WHERE u.IS_BOT = 0 AND u.ETH_ADDRESS IS NOT NULL AND u.CREATED_AT IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM product_events e WHERE e.NAME = 'agent_created' AND e.AGENT_ADDRESS = LOWER(u.ETH_ADDRESS)
          )
        """
    ).fetchall()
    conn.cursor().executemany(
        f"{_INSERT} VALUES (%s, 'agent_created', %s, %s, %s) ON CONFLICT DO NOTHING",
        [
            (
                row["CREATED_AT"],
                row["OWNER_WORKOS_ID"],
                row["ADDRESS"],
                json.dumps(
                    {
                        "runner": runner_for(row["AGENT_APP"], row["AGENT_HOST"]).slug,
                        "source": "mcp" if row["AGENT_APP"] else "api_key",
                        "agents_before": row["AGENTS_BEFORE"],
                    }
                ),
            )
            for row in agents
        ],
    )
    conn.execute(
        f"""
        {_INSERT}
        SELECT f.MATCH_TIME, 'first_trade', {_ACCOUNT_OWNER}, LOWER(u.ETH_ADDRESS),
               jsonb_build_object('market', COALESCE(m.SLUG, f.MARKET), 'agent_age_s', f.MATCH_TIME - u.CREATED_AT)
        FROM users u
        JOIN owners o ON o.WORKOS_ID = {_ACCOUNT_OWNER}
        CROSS JOIN LATERAL (
            SELECT t.STATUS, t.MATCH_TIME, t.MARKET FROM trades t
            WHERE t.TAKER_API_KEY = u.API_KEY AND t.STATUS IN ('PENDING', 'CONFIRMED')
            ORDER BY t.MATCH_TIME LIMIT 1
        ) f
        LEFT JOIN markets m ON m.CONDITION_ID = f.MARKET
        WHERE f.STATUS = 'CONFIRMED' AND u.IS_BOT = 0 AND u.ETH_ADDRESS IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM product_events e WHERE e.NAME = 'first_trade' AND e.AGENT_ADDRESS = LOWER(u.ETH_ADDRESS)
          )
        ON CONFLICT DO NOTHING
        """
    )
    conn.execute(
        f"""
        {_INSERT}
        SELECT d.FIRST_FILL, 'active_day', {_ACCOUNT_OWNER}, LOWER(u.ETH_ADDRESS), '{{}}'::jsonb
        FROM users u
        JOIN owners o ON o.WORKOS_ID = {_ACCOUNT_OWNER}
        CROSS JOIN LATERAL (
            SELECT MIN(t.MATCH_TIME) AS FIRST_FILL FROM trades t
            WHERE t.TAKER_API_KEY = u.API_KEY AND t.STATUS = 'CONFIRMED'
              AND t.MATCH_TIME >= (
                SELECT (COALESCE(MAX(e.AT), 0) / 86400 - 2) * 86400 FROM product_events e WHERE e.NAME = 'active_day'
              )
            GROUP BY t.MATCH_TIME / 86400
        ) d
        WHERE u.IS_BOT = 0 AND u.ETH_ADDRESS IS NOT NULL
        ON CONFLICT DO NOTHING
        """
    )
    conn.execute(
        f"""
        {_INSERT}
        SELECT ce.ENROLLED_AT, 'competition_entered', ce.OWNER, LOWER(u.ETH_ADDRESS),
               jsonb_build_object('competition', ce.SLUG)
        FROM competition_entries ce
        JOIN users u ON u.USER_ID = ce.USER_ID AND u.OWNER_WORKOS_ID = ce.OWNER
        JOIN owners o ON o.WORKOS_ID = ce.OWNER
        WHERE ce.ENROLLED_AT IS NOT NULL AND u.IS_BOT = 0 AND u.ETH_ADDRESS IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM product_events e
            WHERE e.NAME = 'competition_entered' AND e.AGENT_ADDRESS = LOWER(u.ETH_ADDRESS)
              AND e.PROPS->>'competition' = ce.SLUG AND e.AT = ce.ENROLLED_AT
          )
        """
    )


def owner_hash(secret: str, workos_id: str) -> str:
    return hmac.new(secret.encode(), workos_id.encode(), hashlib.sha256).hexdigest()[:32]


def _timestamp(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Forwarder:
    """Derives milestones, sends unsent events to PostHog and erases deleted
    people there. Each event's uuid comes from its row id, so a resend is
    deduplicated. Anything not delivered stays queued for the next round."""

    def __init__(
        self,
        db: DbSession,
        *,
        project_token: str,
        personal_api_key: str,
        project_id: str,
        id_secret: str,
        interval_s: float = 60.0,
        client: httpx.Client | None = None,
    ):
        self._db = db
        self._project_token = project_token
        self._personal_api_key = personal_api_key
        self._erase_url = ERASE_URL.format(project_id=project_id)
        self._secret = id_secret
        self._interval = interval_s
        self._client = client or httpx.Client(timeout=20.0)

    async def run(self) -> None:
        """Sync every interval. While a round fails, wait twice as long each
        time, up to 15 minutes, and log the traceback only once."""
        failures = 0
        while True:
            try:
                await asyncio.to_thread(self.sync, failures + 1)
                if failures:
                    log.info("product events: forwarding recovered after %d failed attempts", failures)
                failures = 0
            except Exception as exc:
                if failures == 0:
                    log.exception("product events: forwarding failed; will retry with backoff")
                else:
                    log.warning("product events: forwarding still failing (%s)", type(exc).__name__)
                failures += 1
            await asyncio.sleep(min(self._interval * 2 ** min(failures, 10), MAX_BACKOFF_S))

    def sync(self, attempt: int = 1) -> None:
        self.erase()
        with self._db.write() as conn:
            derive(conn)
        while self.flush(attempt) == BATCH:
            pass

    def flush(self, attempt: int = 1) -> int:
        """Send one batch of the oldest unsent events and mark what PostHog
        settled. The number marked. No transaction stays open across HTTP."""
        now = int(time.time())
        with self._db.read() as conn:
            rows = cast(
                list[Row],
                conn.execute(
                    """
                    SELECT e.ID, e.AT, e.NAME, e.OWNER_WORKOS_ID, e.AGENT_ADDRESS, e.PROPS,
                           o.SIGNUP_VIA, o.FIRST_REF, o.UTM_SOURCE, o.UTM_MEDIUM, o.UTM_CAMPAIGN
                    FROM product_events e LEFT JOIN owners o ON o.WORKOS_ID = e.OWNER_WORKOS_ID
                    WHERE e.SENT_AT IS NULL
                    ORDER BY e.ID LIMIT %s
                    """,
                    (BATCH,),
                ).fetchall(),
            )
        marked = 0
        for historical in (False, True):
            part = [r for r in rows if (now - r["AT"] > HISTORICAL_AFTER_S) == historical]
            if not part:
                continue
            delivered = self._send(part, historical, attempt)
            with self._db.write() as conn:
                conn.execute("UPDATE product_events SET SENT_AT = %s WHERE ID = ANY(%s)", (now, delivered))
            marked += len(delivered)
        return marked

    def erase(self) -> None:
        """Erase people who deleted their accounts, once any batch in flight
        when they did has landed. PostHog deletes only events it already has."""
        with self._db.read() as conn:
            queued = cast(
                list[Row],
                conn.execute(
                    "SELECT OWNER_WORKOS_ID FROM product_event_erasures WHERE AT <= %s ORDER BY AT LIMIT %s",
                    (int(time.time()) - ERASE_AFTER_S, ERASE_BATCH),
                ).fetchall(),
            )
        owners = [row["OWNER_WORKOS_ID"] for row in queued]
        if not owners:
            return
        response = self._client.post(
            self._erase_url,
            headers={"Authorization": f"Bearer {self._personal_api_key}"},
            json={"distinct_ids": [owner_hash(self._secret, owner) for owner in owners], "delete_events": True},
        )
        response.raise_for_status()
        if errors := response.json().get("deletion_errors"):
            raise RuntimeError(f"PostHog could not erase {len(errors)} persons")
        with self._db.write() as conn:
            conn.execute("DELETE FROM product_event_erasures WHERE OWNER_WORKOS_ID = ANY(%s)", (owners,))

    def _send(self, rows: list[Row], historical: bool, attempt: int) -> list[int]:
        """Capture one batch. The ids of the rows PostHog settled: stored, or
        dropped for good. An event it asks to retry stays unsent."""
        events = {str(uuid.uuid5(_UUIDS, str(row["ID"]))): row for row in rows}
        sent_at = _timestamp(time.time())
        body: Row = {
            "created_at": sent_at,
            "batch": [self._event(event_id, row) for event_id, row in events.items()],
        }
        if historical:
            body["historical_migration"] = True
        response = self._client.post(
            CAPTURE_URL,
            json=body,
            headers={
                "Authorization": f"Bearer {self._project_token}",
                "PostHog-Sdk-Info": "agentpit-server/1",
                "PostHog-Request-Id": str(uuid.uuid5(_UUIDS, ",".join(events))),
                "PostHog-Attempt": str(attempt),
                "PostHog-Request-Timestamp": sent_at,
            },
        )
        response.raise_for_status()
        results = response.json()["results"]
        delivered = []
        for event_id, row in events.items():
            result = results.get(event_id, {})
            if result.get("result") == "drop":
                log.warning("PostHog dropped product event %s: %s", row["NAME"], result.get("details"))
            if result.get("result") in DELIVERED:
                delivered.append(row["ID"])
        return delivered

    def _event(self, event_id: str, row: Row) -> Row:
        person = {key: row[column] for key, column in _PERSON_PROPS if row[column]}
        properties: Row = {
            **row["PROPS"],
            **person,
            "$set_once": person,
            "$geoip_disable": True,
            "$lib": "agentpit-server",
        }
        if row["AGENT_ADDRESS"]:
            properties["agent_address"] = row["AGENT_ADDRESS"]
        return {
            "event": row["NAME"],
            "uuid": event_id,
            "distinct_id": owner_hash(self._secret, row["OWNER_WORKOS_ID"]),
            "timestamp": _timestamp(row["AT"]),
            "properties": properties,
        }
