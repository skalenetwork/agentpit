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
from agentpit.domain.exceptions import HandleAlreadyExistsError, OnboardingError, UserNotFoundError
from agentpit.domain.handles import pick_handle
from agentpit.domain.runner import is_clawbits

Onboard = Callable[[str, LocalAccount], User]


class AgentAccounts:
    def __init__(self, db: DbSession, onboard: Onboard) -> None:
        self._db = db
        self._onboard = onboard
        self._onboarding = threading.Lock()

    def agent_for(self, owner_workos_id: str, app: str, client: str | None, host: str | None) -> User:
        with self._db.read() as conn:
            agent = TableRead.get_agent(conn, owner_workos_id, app, client)
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
        try:
            return self.ready(created)
        except OnboardingError:
            with self._db.write() as conn:
                TableWrite.delete_agent(conn, user_id, int(time.time()))
            raise

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
        with self._onboarding:
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
        except UniqueViolation:
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
        TableWrite.set_auto_redeem(conn, user_id, True)
        return user_id
