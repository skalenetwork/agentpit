"""Shared helpers for the live-anvil test suite.

Each test creates its own app + clients so it's isolated from the singleton
app built by tests/conftest.py with on-chain disabled.
"""

import secrets
import uuid

from fastapi.testclient import TestClient

from agentpit.api.app import create_app
from agentpit.api.deps import (
    get_db_session,
    get_google_verifier,
    get_jwt_coder,
    get_onchain_admin,
    get_settings,
    get_workos_client,
)
from agentpit.datastructures.register_request import RegisterRequest
from agentpit.datastructures.user import User
from agentpit.db.table_read import TableRead
from agentpit.liquidity.house_accounts import HouseAccountProvisioner
from agentpit.services.order_service import OrderService
from agentpit.services.auth_service import AuthService

# AGENTPIT_ADMIN_TOKEN is read at app startup by Settings; tests rely on
# the default ("dev-admin-token") so we don't need to mutate env here.
ADMIN_HDR = {"X-Admin-Token": "dev-admin-token"}


def fresh_client() -> TestClient:
    return TestClient(create_app())


def hdr(token: str) -> dict[str, str]:
    """The credential these tests carry.

    Since the WorkOS cutover the browser credential is an AuthKit access
    token, which no test can mint without talking to WorkOS. The API accepts
    a long-lived `X-API-Key` on exactly the same routes -- it is how every
    trading bot authenticates -- so that is what the suite uses.
    """
    return {"X-API-Key": token}


def unique_email() -> str:
    return f"e2e-{uuid.uuid4().hex[:10]}@example.com"


def unique_question() -> str:
    return f"Test market {secrets.token_hex(4)}?"


def _auth_service(client: TestClient) -> AuthService:
    """An AuthService built from the very dependencies the app under test is
    using, so a test account is onboarded against the same chain and database
    as the requests that follow."""
    overrides = client.app.dependency_overrides  # type: ignore[attr-defined]
    return AuthService(
        overrides[get_db_session](),
        overrides[get_jwt_coder](),
        overrides[get_onchain_admin](),
        overrides[get_settings](),
        overrides[get_google_verifier](),
        overrides[get_workos_client](),
    )


def register(client: TestClient, email: str | None = None) -> dict:
    """A funded, approved account, shaped like the old POST /register reply.

    `POST /register` went away with the WorkOS cutover -- there is no
    programmatic signup any more -- but the service behind it is untouched and
    is still the only place that provisions a wallet, grants gas and sets the
    exchange approvals. Tests call it directly and authenticate with the
    account's API key; `access_token` keeps its name so the suite reads the
    same either side of the cutover.
    """
    address = email or unique_email()
    service = _auth_service(client)
    response = service.register(
        RegisterRequest(email=address, password="hunter22hunter22")
    )
    db = client.app.dependency_overrides[get_db_session]()  # type: ignore[attr-defined]
    with db.read() as conn:
        user = TableRead.get_user_by_email_ci(conn, address)
    assert user is not None, "register() did not persist the account"
    return {
        "access_token": user.api_key,
        "api_key": user.api_key,
        "user": response.user.model_dump(),
    }


def create_market(client: TestClient, question: str | None = None, *, active: bool = True) -> dict:
    market = client.post(
        "/markets",
        json={
            "question": question or unique_question(),
            "description": "test",
            "outcome_labels": ["YES", "NO"],
        },
        headers=ADMIN_HDR,
    ).json()
    if active:
        return client.post(f"/markets/{market['market_id']}/activate", headers=ADMIN_HDR).json()
    return market


def house(client: TestClient) -> User:
    overrides = client.app.dependency_overrides  # type: ignore[attr-defined]
    return HouseAccountProvisioner(
        overrides[get_db_session](),
        overrides[get_onchain_admin](),
        overrides[get_settings](),
    ).ensure_provisioned()


def order_service(client: TestClient) -> OrderService:
    overrides = client.app.dependency_overrides  # type: ignore[attr-defined]
    return OrderService(overrides[get_db_session](), overrides[get_onchain_admin]())
