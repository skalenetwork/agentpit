"""Shared helpers for the live-anvil test suite.

Each test creates its own app + clients so it's isolated from the singleton
app built by tests/conftest.py with on-chain disabled.
"""

import json
import secrets
import time
import uuid

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
    """Give a `register()`ed account gas to sign its own transactions.

    Onboarding leaves none (the product never has a user sign outside
    `UserGasSponsor`), and on anvil the leftover would cover one split only by
    accident, since anvil bills the 7 wei base fee, not the price offered.
    Sent by the app's own admin, so its `AdminTxSender` stays the only writer
    of the admin nonce.
    """
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
    split, merge or claim from a test is sponsored as the API's would be.
    `settings` defaults to the environment's."""
    settings = settings or Settings()
    return PositionService(db, admin, UserGasSponsor(db, admin, settings))


def drain_native_balance(admin: OnchainAdmin, address: str) -> None:
    """Set `address`'s native balance to exactly 0: the wallet the sponsor tops
    up from nothing. `anvil_setBalance`, because a real send-to-zero overpays
    (EIP-1559's effective price is only known after the block mines)."""
    admin._client.web3.provider.make_request(  # noqa: SLF001
        "anvil_setBalance", [Web3.to_checksum_address(address), "0x0"]
    )


# Gas a house account gets in the on-chain tests instead of the default 5
# native: conftest truncates `users` before every test, so the house tests
# provision a dozen accounts per run, all paid by the persistent anvil's admin
# (~60 native a run at the default floor, ~0.6 at this one).
HOUSE_TEST_GAS_FLOOR_WEI = 5 * 10**16


# --- scenarios on the local chain, shared by the sponsored-gas tests ---------


def local_admin() -> OnchainAdmin:
    """An admin on the local deployment, with no app around it."""
    settings = Settings()
    deployment = Deployment.load(settings.deployment_path)
    client = Web3Client(settings, deployment)
    return OnchainAdmin(client, Contracts(client.web3, deployment))


def chain() -> tuple[OnchainAdmin, DbSession]:
    """`local_admin()` and a pool on the test database."""
    return local_admin(), fresh_test_db()


def synced_market(db: DbSession, admin: OnchainAdmin) -> tuple[Market, dict]:
    """A binary market prepared on the local CTF and ACTIVE, plus the upstream
    document it was synced from (what `resolve_yes` later mirrors)."""
    suffix = secrets.token_hex(4)
    pm = {
        "id": int(secrets.token_hex(4), 16),
        "conditionId": "0x" + secrets.token_hex(32),
        "question": f"Sponsored claim {suffix}?",
        "description": "d",
        "slug": f"sponsored-claim-{suffix}",
        "startDate": "2020-01-01T00:00:00Z",
        "endDate": "2020-01-02T00:00:00Z",
        "active": True,
        "closed": False,
        "tokens": [
            {"token_id": str(int(secrets.token_hex(8), 16)), "outcome": "Yes"},
            {"token_id": str(int(secrets.token_hex(8), 16)), "outcome": "No"},
        ],
    }
    with db.write() as conn:
        return create_polymarket_markets_if_needed(conn, [pm], admin)[0], pm


def resolve_yes(db: DbSession, admin: OnchainAdmin, *pms: dict) -> None:
    """Mirror an upstream YES win for each of `pms`: `reportPayouts` on chain,
    then RESOLVED in the database."""
    won = {
        pm["conditionId"]: dict(
            pm, closed=True, tokens=[dict(t, winner=(i == 0)) for i, t in enumerate(pm["tokens"])]
        )
        for pm in pms
    }
    with db.write() as conn:
        mirror_polymarket_resolutions(conn, admin, fetcher=won.get, now=9_999_999_999)


def new_account(db: DbSession, *, auto_redeem: bool | None = None) -> User:
    """A fresh account that has never held native coin or sent a transaction.
    `auto_redeem` sets the opt-in explicitly; left out, the column default."""
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


def onboarded_account(
    db: DbSession, admin: OnchainAdmin, *, auto_redeem: bool | None = None
) -> User:
    """`new_account`, onboarded the way signup does it: the faucet's apUSD and
    the three approvals, sent after one sponsored top-up. The wallet ends up
    holding at most what those approvals needed."""
    user = new_account(db, auto_redeem=auto_redeem)
    admin.faucet_drip(user.eth_address)
    sponsor = UserGasSponsor(db, admin, Settings())
    with sponsor.locked(user):
        sponsor.send(user, admin.approval_calls(), "onboarding")
    return user


def split(db: DbSession, admin: OnchainAdmin, user: User, market: Market, amount: int) -> None:
    position_service(db, admin).split(user, market.market_id, SplitPositionRequest(amount=amount))


def dry_winner(
    db: DbSession, admin: OnchainAdmin, amount: int = 100_000_000
) -> tuple[Market, User]:
    """`(market, user)`: `user` split `amount` on a market whose YES side has
    won since, and holds no native coin -- the claim that needs a top-up."""
    market, pm = synced_market(db, admin)
    user = onboarded_account(db, admin)
    split(db, admin, user, market, amount)
    resolve_yes(db, admin, pm)
    drain_native_balance(admin, user.eth_address)
    return market, user


def send_as(admin: OnchainAdmin, user: User, fn) -> None:
    """`user` signs `fn` itself, on gas given for the purpose. Through the
    class, so a test's hook on `admin.fund_gas` does not see this top-up."""
    OnchainAdmin.fund_gas(admin, user.eth_address, 10**16)
    assert send_user_tx(admin._client, user.eth_key, fn)["status"] == 1  # noqa: SLF001


def give_tokens(admin: OnchainAdmin, sender: User, to: str, token_id: int, amount: int) -> None:
    """`sender` signs a transfer of `amount` of one outcome token to `to`."""
    transfer = admin._contracts.ctf.functions.safeTransferFrom(  # noqa: SLF001
        Web3.to_checksum_address(sender.eth_address),
        Web3.to_checksum_address(to),
        token_id,
        amount,
        b"",
    )
    send_as(admin, sender, transfer)


def sponsored_gas(db: DbSession, user: User) -> int:
    """Gas booked to `user`'s `sponsored_gas` row for today."""
    with db.read() as conn:
        return TableRead.sponsored_gas_used(conn, user.api_key, int(time.time()) // 86_400)


def tx_rows(db: DbSession, user: User, kind: str) -> int:
    """How many `kind` rows (SPLIT, REDEEM, ...) `user` has in its history."""
    with db.read() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS N FROM transactions "
            "WHERE API_KEY = %s AND TRANSACTION_TYPE = %s",
            (user.api_key, kind),
        ).fetchone()["N"]


def redeem_amounts(db: DbSession, user: User) -> list[int]:
    """`collateral_amount` of each REDEEM row: what the profile page reads."""
    with db.read() as conn:
        return [
            json.loads(r["DETAILS"])["collateral_amount"]
            for r in conn.execute(
                "SELECT DETAILS FROM transactions "
                "WHERE API_KEY = %s AND TRANSACTION_TYPE = 'REDEEM'",
                (user.api_key,),
            ).fetchall()
        ]


def pending_user_txs(db: DbSession) -> list[tuple[str, str, str, int | None, dict]]:
    """Every intent row still waiting on its receipt: (hash, key, type, market, details)."""
    with db.read() as conn:
        return [
            (r.tx_hash, r.api_key, r.transaction_type, r.market_id, r.details)
            for r in TableRead.list_pending_user_txs(conn)
        ]
