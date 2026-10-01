import anyio
import jwt
from mcp.server.auth.provider import AccessToken

from agentpit.auth.authkit_tokens import KeyResolver
from agentpit.auth.workos_client import ConnectApp, WorkOsClient
from agentpit.datastructures.user import User
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.domain.exceptions import InvalidCredentialsError
from agentpit.domain.runner import is_clawbits
from agentpit.domain.text import clean
from agentpit.services.agent_accounts import AgentAccounts


class AgentToken(AccessToken):
    user: User


class AgentVerifier:
    def __init__(
        self,
        *,
        issuer: str,
        resource: str,
        resolve: KeyResolver,
        workos: WorkOsClient,
        accounts: AgentAccounts,
        db: DbSession,
    ) -> None:
        self._issuer = issuer
        self._resource = resource
        self._resolve = resolve
        self._workos = workos
        self._accounts = accounts
        self._db = db
        self._apps: dict[str, ConnectApp] = {}

    async def verify_token(self, token: str) -> AgentToken | None:
        return await anyio.to_thread.run_sync(self._verify, token)

    def _verify(self, token: str) -> AgentToken | None:
        user = self._oauth_user(token) if token.count(".") == 2 else self._key_user(token)
        if user is None:
            return None
        return AgentToken(token=token, client_id=user.user_id, scopes=[], resource=self._resource, user=user)

    def _key_user(self, key: str) -> User | None:
        with self._db.read() as conn:
            user = TableRead.get_user_by_api_key(conn, key)
        return user if user is not None and user.deleted_at is None else None

    def _oauth_user(self, token: str) -> User | None:
        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
            if unverified.get("iss") != self._issuer or unverified.get("aud") != self._resource:
                return None
            claims = jwt.decode(
                token,
                self._resolve(token),
                algorithms=["RS256"],
                issuer=self._issuer,
                audience=self._resource,
                options={"require": ["exp", "iss", "aud", "sub", "client_id"]},
            )
        except (jwt.PyJWTError, InvalidCredentialsError):
            return None
        app = self._app(claims["client_id"])
        client = claims["client_id"] if is_clawbits(app.host) else None
        return self._accounts.agent_for(claims["sub"], app.name, client, app.host)

    def _app(self, client_id: str) -> ConnectApp:
        if client_id not in self._apps:
            app = self._workos.application(client_id)
            self._apps[client_id] = ConnectApp(name=clean(app.name, 40), host=app.host)
        return self._apps[client_id]
