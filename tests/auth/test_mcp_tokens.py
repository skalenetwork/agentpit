import time
from typing import Any

import anyio
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from agentpit.auth.mcp_tokens import AgentToken, AgentVerifier
from agentpit.auth.workos_client import FakeWorkOsClient
from agentpit.services.agent_accounts import AgentAccounts
from tests.db_helpers import fresh_test_db

ISSUER = "https://test.authkit.app"
MCP_URL = "https://api.agentpit.dev/mcp"
CLIENT_ID = "https://claude.ai/oauth/claude-code-client-metadata"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class Resolver:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, _token: str) -> RSAPublicKey:
        self.calls += 1
        return KEY.public_key()


def _token(**over: Any) -> str:
    now = int(time.time())
    claims = {"iss": ISSUER, "aud": MCP_URL, "sub": "user_01", "client_id": CLIENT_ID, "iat": now, "exp": now + 300}
    return jwt.encode({**claims, **over}, KEY, algorithm="RS256")


@pytest.fixture
def world() -> tuple[AgentVerifier, AgentAccounts, Resolver]:
    db = fresh_test_db()
    accounts = AgentAccounts(db, lambda *_: pytest.fail("the verifier must not onboard"))
    workos = FakeWorkOsClient()
    workos.applications[CLIENT_ID] = "Claude Code"
    workos.applications["client_long"] = "Ignore\nall   previous " + "x" * 80
    resolver = Resolver()
    verifier = AgentVerifier(
        issuer=ISSUER, resource=MCP_URL, resolve=resolver, workos=workos, accounts=accounts, db=db
    )
    return verifier, accounts, resolver


def _verify(verifier: AgentVerifier, token: str) -> AgentToken | None:
    return anyio.run(verifier.verify_token, token)


def test_a_valid_token_resolves_the_owner_s_agent_for_that_app(world: tuple[AgentVerifier, AgentAccounts, Resolver]):
    verifier, accounts, _ = world

    token = _verify(verifier, _token())

    assert token is not None
    assert token.resource == MCP_URL
    assert token.user.agent_app == "Claude Code"
    assert token.user.user_id == accounts.agent_for("user_01", "Claude Code").user_id


@pytest.mark.parametrize(
    ("over", "fetches"),
    [
        ({"aud": "https://elsewhere.example/mcp"}, 0),
        ({"iss": "https://evil.authkit.app"}, 0),
        ({"exp": int(time.time()) - 10}, 1),
    ],
    ids=["wrong-aud", "wrong-iss", "expired"],
)
def test_a_foreign_or_stale_token_is_refused(
    world: tuple[AgentVerifier, AgentAccounts, Resolver], over: dict[str, Any], fetches: int
):
    verifier, _, resolver = world

    assert _verify(verifier, _token(**over)) is None
    assert resolver.calls == fetches


def test_a_spa_token_without_aud_is_refused_before_any_key_fetch(world: tuple[AgentVerifier, AgentAccounts, Resolver]):
    verifier, _, resolver = world
    now = int(time.time())
    spa = jwt.encode(
        {"iss": "https://api.workos.com/user_management/client_01", "sub": "user_01", "client_id": "client_01", "exp": now + 300},
        KEY,
        algorithm="RS256",
    )

    assert _verify(verifier, spa) is None
    assert resolver.calls == 0


def test_an_api_key_resolves_its_account(world: tuple[AgentVerifier, AgentAccounts, Resolver]):
    verifier, accounts, _ = world
    agent = accounts.agent_for("user_01", "Muse")

    token = _verify(verifier, agent.api_key)

    assert token is not None and token.user.user_id == agent.user_id


@pytest.mark.parametrize("token", ["not-a-key", "a.b.c"], ids=["key-miss", "garbage"])
def test_an_unknown_key_or_garbage_is_refused(world: tuple[AgentVerifier, AgentAccounts, Resolver], token: str):
    verifier, _, _ = world

    assert _verify(verifier, token) is None


def test_a_client_chosen_app_name_is_flattened_and_capped(world: tuple[AgentVerifier, AgentAccounts, Resolver]):
    verifier, _, _ = world

    token = _verify(verifier, _token(client_id="client_long"))

    assert token is not None and token.user.agent_app == "Ignore all previous " + "x" * 19 + "…"
