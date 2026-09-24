import threading
from collections.abc import Callable

from eth_account.signers.local import LocalAccount
from psycopg.errors import UniqueViolation

from agentpit.datastructures.user import User
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import UserNotFoundError
from agentpit.domain.handles import pick_handle

Onboard = Callable[[str, LocalAccount], User]


class AgentAccounts:
    def __init__(self, db: DbSession, onboard: Onboard) -> None:
        self._db = db
        self._onboard = onboard
        self._onboarding = threading.Lock()

    def agent_for(self, owner_workos_id: str, app: str) -> User:
        with self._db.read() as conn:
            agent = TableRead.get_agent(conn, owner_workos_id, app)
        return agent or self._create(owner_workos_id, app)

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

    def _create(self, owner_workos_id: str, app: str) -> User:
        try:
            with self._db.write() as conn:
                handle = pick_handle(taken=lambda name: TableRead.handle_taken(conn, name))
                user_id, _, _ = TableWrite.create_user(
                    conn,
                    email=None,
                    password_hash=None,
                    handle=handle,
                    owner_workos_id=owner_workos_id,
                    agent_app=app,
                )
                TableWrite.set_auto_redeem(conn, user_id, True)
                created = TableRead.get_user_by_userid(conn, user_id)
        except UniqueViolation:
            with self._db.read() as conn:
                created = TableRead.get_agent(conn, owner_workos_id, app)
            if created is None:
                raise
        if created is None:
            raise UserNotFoundError()
        return created
