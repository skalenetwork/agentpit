import threading
import time
from collections.abc import Callable

import psycopg
from eth_account.signers.local import LocalAccount
from psycopg.errors import UniqueViolation

from agentpit.datastructures.user import User
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import AgentLimitError, HandleAlreadyExistsError, UserNotFoundError
from agentpit.domain.handles import pick_handle
from agentpit.domain.runner import is_clawbits

Onboard = Callable[[str, LocalAccount], User]

# Each new agent is set up at the operator's expense (a faucet drip, a gas
# top-up and its approvals) and gets its own daily sponsored-gas budget, so
# one owner may have only so many. An agent deleted within a day of its
# creation still counts, or deleting and recreating would get round the cap.
MAX_AGENTS_PER_OWNER = 10
RECENT_AGENT_SECONDS = 86_400


class AgentAccounts:
    def __init__(self, db: DbSession, onboard: Onboard) -> None:
        self._db = db
        self._onboard = onboard
        self._onboarding: dict[str, threading.Lock] = {}

    def agent_for(self, owner_workos_id: str, app: str, client: str | None, host: str | None) -> User | None:
        with self._db.read() as conn:
            agent = TableRead.get_agent(conn, owner_workos_id, app, client)
            person = None if agent else TableRead.get_user_by_workos_id(conn, owner_workos_id)
        if person is not None and person.deleted_at is not None:
            return None
        if agent is None and client is not None:
            agent = self._adopt(owner_workos_id, app, client)
        if agent is None:
            return self._create(owner_workos_id, app, client, host)
        if agent.agent_host != host:
            with self._db.write() as conn:
                TableWrite.set_agent_host(conn, agent.user_id, host)
            return agent.model_copy(update={"agent_host": host})
        return agent

    def create_api_agent(self, owner_workos_id: str) -> User:
        with self._db.write() as conn:
            user_id = self._insert(conn, owner_workos_id, None, None, None)
            created = TableRead.get_user_by_userid(conn, user_id)
        if created is None:
            raise UserNotFoundError()
        return created

    def rename(self, agent: User, handle: str) -> User:
        try:
            with self._db.write() as conn:
                TableWrite.update_user_handle(conn, agent.user_id, handle)
        except UniqueViolation as exc:
            raise HandleAlreadyExistsError(handle) from exc
        return agent.model_copy(update={"handle": handle})

    def ready(self, user: User) -> User:
        if user.onboarded_at is not None:
            return user
        with self._onboarding.setdefault(user.user_id, threading.Lock()):
            with self._db.read() as conn:
                fresh = TableRead.get_user_by_userid(conn, user.user_id)
            if fresh is None:
                raise UserNotFoundError()
            if fresh.onboarded_at is not None:
                return fresh
            return self._onboard(fresh.user_id, fresh.eth_key)

    def _adopt(self, owner_workos_id: str, app: str, client: str) -> User | None:
        with self._db.write() as conn:
            legacy = TableRead.get_agent(conn, owner_workos_id, app, None)
            if legacy is None or not is_clawbits(legacy.agent_host):
                return None
            if not TableWrite.adopt_agent(conn, legacy.user_id, client):
                return None
            return TableRead.get_user_by_userid(conn, legacy.user_id)

    def _create(self, owner_workos_id: str, app: str, client: str | None, host: str | None) -> User:
        try:
            with self._db.write() as conn:
                user_id = self._insert(conn, owner_workos_id, app, client, host)
                created = TableRead.get_user_by_userid(conn, user_id)
        except (UniqueViolation, AgentLimitError):
            # Lost to a request for the same app: the unique index refused the
            # duplicate, or the winner took the owner's last slot while this
            # one waited on the owner lock. Either way its agent exists now.
            with self._db.read() as conn:
                created = TableRead.get_agent(conn, owner_workos_id, app, client)
            if created is None:
                raise
        if created is None:
            raise UserNotFoundError()
        return created

    @staticmethod
    def _insert(
        conn: psycopg.Connection, owner_workos_id: str, app: str | None, client: str | None, host: str | None
    ) -> str:
        since = int(time.time()) - RECENT_AGENT_SECONDS
        # Refused before the lock below when already at the cap: an MCP app
        # with no room comes back on every request, and must not queue behind
        # the owner's other traffic each time.
        AgentAccounts._refuse_at_cap(conn, owner_workos_id, since)
        TableWrite.ensure_owner(conn, owner_workos_id)
        # Held until this transaction commits, so two creations for one owner
        # cannot both pass the second count.
        TableWrite.lock_owner(conn, owner_workos_id)
        AgentAccounts._refuse_at_cap(conn, owner_workos_id, since)
        handle = pick_handle(taken=lambda name: TableRead.handle_taken(conn, name))
        user_id, _, _ = TableWrite.create_user(
            conn,
            email=None,
            password_hash=None,
            handle=handle,
            owner_workos_id=owner_workos_id,
            agent_app=app,
            agent_host=host,
            agent_client=client,
        )
        return user_id

    @staticmethod
    def _refuse_at_cap(conn: psycopg.Connection, owner_workos_id: str, since: int) -> None:
        if TableRead.count_owner_agents(conn, owner_workos_id, since) >= MAX_AGENTS_PER_OWNER:
            raise AgentLimitError(
                f"at most {MAX_AGENTS_PER_OWNER} agents per account, and each counts for 24 hours after it was "
                "created even once deleted: delete one created more than a day ago at app.agentpit.dev/settings, "
                "or try again later"
            )
