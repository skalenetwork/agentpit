"""Shared helpers for the live-anvil test suite.

Each test creates its own app + clients so it's isolated from the singleton
app built by tests/conftest.py with on-chain disabled.
"""

import secrets
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from web3 import Web3

from agentpit.api.app import create_app
from agentpit.api.deps import (
    get_db_session,
    get_google_verifier,
    get_jwt_coder,
    get_onchain_admin,
    get_settings,
)
from agentpit.config import Settings
from agentpit.datastructures.market import Market
from agentpit.datastructures.register_request import RegisterRequest
from agentpit.datastructures.split_position_request import SplitPositionRequest
from agentpit.datastructures.user import User
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.onchain.admin import OnchainAdmin
from agentpit.onchain.contracts import Contracts
from agentpit.onchain.deployment import Deployment
from agentpit.onchain.user_wallet import send_user_tx
from agentpit.onchain.web3_client import Web3Client
from agentpit.polymarket.polymarket_sync import (
    create_polymarket_markets_if_needed,
    mirror_polymarket_resolutions,
)
from agentpit.services.auth_service import AuthService
from agentpit.services.gas_sponsor import UserGasSponsor
from agentpit.services.position_service import PositionService
from tests.db_helpers import fresh_test_db

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
    as the requests that follow (its sponsor included)."""
    overrides = client.app.dependency_overrides  # type: ignore[attr-defined]
    return AuthService(
        overrides[get_db_session](),
        overrides[get_jwt_coder](),
        overrides[get_onchain_admin](),
        overrides[get_settings](),
        overrides[get_google_verifier](),
    )


def register(client: TestClient, email: str | None = None) -> dict:
    """An onboarded account -- collateral dripped, approvals set -- shaped
    like the old POST /register reply.

    `POST /register` went away with the WorkOS cutover -- there is no
    programmatic signup any more -- but the service behind it is untouched and
    is still the only place that provisions a wallet, drips its collateral and
    sets the exchange approvals. Tests call it directly and authenticate with
    the account's API key; `access_token` keeps its name so the suite reads the
    same either side of the cutover.

    The wallet keeps no spare gas (onboarding tops it up to exactly the
    approvals' need): a test that signs as the account itself calls
    `fund_direct_sends` first.
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


def fund_direct_sends(client: TestClient, address: str) -> None:
    """Give a `register()`ed account gas to sign its own transactions: onboarding leaves none, and
    on anvil its leftover would cover one split only by accident (it bills the 7 wei base fee).
    Sent by the app's own admin, so its `AdminTxSender` stays the only nonce writer."""
    admin = client.app.dependency_overrides[get_onchain_admin]()  # type: ignore[attr-defined]
    admin.fund_gas(address, 10**16)  # ~10M gas at anvil's ~1 gwei; a split's limit is ~132k


def create_market(client: TestClient, question: str | None = None, *, state: str = "ACTIVE") -> dict:
    return client.post(
        "/markets",
        json={
            "question": question or unique_question(),
            "description": "test",
            "outcome_labels": ["YES", "NO"],
            "state": state,
        },
        headers=ADMIN_HDR,
    ).json()


def position_service(
    db: DbSession, admin: OnchainAdmin, settings: Settings | None = None
) -> PositionService:
    """A PositionService wired as `deps.get_position_service` wires one, so a
    split, merge or claim from a test is sponsored as the API's would be."""
    return PositionService(db, admin, UserGasSponsor(db, admin, settings or Settings()))


def drain_native_balance(admin: OnchainAdmin, address: str) -> None:
    """Set `address`'s native balance to exactly 0, for a wallet the sponsor tops
    up from nothing. `anvil_setBalance`: a real send-to-zero overpays, as
    EIP-1559's effective price is only known after the block mines."""
    admin._client.web3.provider.make_request(  # noqa: SLF001
        "anvil_setBalance", [Web3.to_checksum_address(address), "0x0"]
    )


# Gas a house account gets in the on-chain tests instead of the default 5 native: conftest
# truncates `users` before every test, so they provision a dozen accounts per run, all
# paid by the persistent anvil's admin.
HOUSE_TEST_GAS_FLOOR_WEI = 5 * 10**16


# --- scenarios on the local chain; `admin` and `db` are fixtures for the tests that import them ---


def local_admin() -> OnchainAdmin:
    """An admin on the local deployment, with no app around it."""
    settings = Settings()
    deployment = Deployment.load(settings.deployment_path)
    client = Web3Client(settings, deployment)
    return OnchainAdmin(client, Contracts(client.web3, deployment))


@pytest.fixture
def admin() -> OnchainAdmin:
    return local_admin()


@pytest.fixture
def db() -> DbSession:
    return fresh_test_db()


def app_world(settings: Settings | None = None):
    """`(client, admin, db)` of the real app. No lifespan, so no background pass
    reconciles anything behind a test's back; a server error is a 500 reply."""
    client = TestClient(create_app(settings), raise_server_exceptions=False)
    overrides = client.app.dependency_overrides  # type: ignore[attr-defined]
    return client, overrides[get_onchain_admin](), overrides[get_db_session]()


def synced_market(db: DbSession, admin: OnchainAdmin) -> tuple[Market, dict]:
    """An ACTIVE binary market on the local CTF, plus the upstream document it
    was synced from (what `resolve_yes` later mirrors)."""
    suffix = secrets.token_hex(4)
    pm = {
        "id": secrets.randbits(32),
        "conditionId": "0x" + secrets.token_hex(32),
        "question": f"Sponsored claim {suffix}?",
        "description": "d",
        "slug": f"sponsored-claim-{suffix}",
        "startDate": "2020-01-01T00:00:00Z",
        "endDate": "2020-01-02T00:00:00Z",
        "active": True,
        "closed": False,
        "tokens": [{"token_id": str(secrets.randbits(64)), "outcome": o} for o in ("Yes", "No")],
    }
    with db.write() as conn:
        return create_polymarket_markets_if_needed(conn, [pm], admin)[0], pm


def resolve_yes(db: DbSession, admin: OnchainAdmin, *pms: dict) -> None:
    """Mirror an upstream YES win for each of `pms`: `reportPayouts` on chain, then RESOLVED."""
    upstream = {}
    for pm in pms:
        tokens = [dict(t, winner=(i == 0)) for i, t in enumerate(pm["tokens"])]
        upstream[pm["conditionId"]] = dict(pm, closed=True, tokens=tokens)
    with db.write() as conn:
        mirror_polymarket_resolutions(conn, admin, fetcher=upstream.get, now=9_999_999_999)


def new_account(db: DbSession, *, auto_redeem: bool | None = None) -> User:
    """A fresh account that has never held native coin or sent a transaction;
    `auto_redeem` sets the opt-in explicitly (left out: the column default)."""
    with db.write() as conn:
        user_id, _acct, _key = TableWrite.create_user(
            conn, email=unique_email(), password_hash="x", handle=None
        )
        if auto_redeem is not None:
            TableWrite.set_auto_redeem(conn, user_id, auto_redeem)
    with db.read() as conn:
        user = TableRead.get_user_by_userid(conn, user_id)
    assert user is not None
    return user


def onboarded_account(db: DbSession, admin: OnchainAdmin, *, auto_redeem=None) -> User:
    """`new_account`, onboarded the way signup does it: the faucet's apUSD, then
    the three approvals sent after one sponsored top-up."""
    user = new_account(db, auto_redeem=auto_redeem)
    admin.faucet_drip(user.eth_address)
    sponsor = UserGasSponsor(db, admin, Settings())
    with sponsor.locked(user):
        sponsor.send(user, admin.approval_calls(), "onboarding")
    return user


def split(db: DbSession, admin: OnchainAdmin, user: User, market: Market, amount: int) -> None:
    position_service(db, admin).split(user, market.market_id, SplitPositionRequest(amount=amount))


def dry_winner(db: DbSession, admin: OnchainAdmin, amount=100_000_000) -> tuple[Market, User]:
    """`(market, user)`: `user` split `amount` on a market whose YES side has
    won since, and holds no native coin -- the claim that needs a top-up."""
    market, pm = synced_market(db, admin)
    user = onboarded_account(db, admin)
    split(db, admin, user, market, amount)
    resolve_yes(db, admin, pm)
    drain_native_balance(admin, user.eth_address)
    return market, user


def send_as(admin: OnchainAdmin, user: User, fn) -> None:
    """`user` signs `fn` itself, on gas given for the purpose. Through the class,
    so a test's hook on `admin.fund_gas` does not see this top-up."""
    OnchainAdmin.fund_gas(admin, user.eth_address, 10**16)
    assert send_user_tx(admin._client, user.eth_key, fn)["status"] == 1  # noqa: SLF001


def give_tokens(admin: OnchainAdmin, sender: User, to: str, token_id: int, amount: int) -> None:
    """`sender` signs a transfer of `amount` of one outcome token to `to`."""
    ctf = admin._contracts.ctf  # noqa: SLF001
    owner, recipient = Web3.to_checksum_address(sender.eth_address), Web3.to_checksum_address(to)
    send_as(admin, sender, ctf.functions.safeTransferFrom(owner, recipient, token_id, amount, b""))


def sponsored_gas(db: DbSession, api_key: str) -> int:
    """Gas booked to the account's `sponsored_gas` row for today."""
    with db.read() as conn:
        return TableRead.sponsored_gas_used(conn, api_key, int(time.time()) // 86_400)


def tx_details(db: DbSession, user: User, kind: str) -> list[dict]:
    """The details of each `kind` row (SPLIT, REDEEM, ...) in `user`'s history."""
    with db.read() as conn:
        history = TableRead.get_transaction_history(conn, user.api_key)
    return [t["details"] for t in history if t["transaction_type"] == kind]


def pending_user_txs(db: DbSession) -> list[tuple[str, str, str, int | None, dict]]:
    """Every intent row still waiting on its receipt: (hash, key, type, market, details)."""
    with db.read() as conn:
        return [
            (r.tx_hash, r.api_key, r.transaction_type, r.market_id, r.details)
            for r in TableRead.list_pending_user_txs(conn)
        ]


def assert_approvals_set(admin: OnchainAdmin, address: str) -> None:
    c = admin._contracts  # noqa: SLF001
    assert c.usd.functions.allowance(address, c.exchange.address).call() == 2**256 - 1
    assert c.usd.functions.allowance(address, c.ctf.address).call() == 2**256 - 1
    assert c.ctf.functions.isApprovedForAll(address, c.exchange.address).call()
