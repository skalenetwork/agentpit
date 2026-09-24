import time
from typing import Any

import jwt

from agentpit.config import Settings


class JwtCoder:
    """Symmetric JWT encode/decode helper bound to the configured secret + algorithm."""

    def __init__(self, settings: Settings):
        self._secret = settings.jwt_secret
        self._algorithm = settings.jwt_algorithm
        self._ttl_seconds = settings.jwt_expires_seconds

    def encode(self, *, user_id: str, email: str | None) -> str:
        now = int(time.time())
        payload = {
            "sub": user_id,
            "email": email,
            "iat": now,
            "exp": now + self._ttl_seconds,
        }
        return jwt.encode(payload, self._secret, algorithm=self._algorithm)

    def decode(self, token: str) -> dict[str, Any]:
        return jwt.decode(token, self._secret, algorithms=[self._algorithm])
