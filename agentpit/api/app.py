import asyncio
import itertools
import logging
import time
from contextlib import asynccontextmanager, nullcontext
from urllib.parse import urlsplit

import httpx
import psycopg
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agentpit.api.deps import (
    get_current_user,
    get_db_session,
    get_google_verifier,
    get_jwt_coder,
    get_onchain_admin,
    get_settings,
    get_workos_client,
)
from agentpit.api.exception_handlers import register_exception_handlers
from agentpit.api.mcp_server import McpEndpoint
from agentpit.api.routes import (
    admin,
    agents,
    auth,
    data_api,
    events,
    leaderboard,
    live,
    market_data,
    markets,
    orders,
    personalities,
    positions,
    system,
    tags,
    usdc,
    users,
)
from agentpit.auth.authkit_tokens import (
    AuthKitVerifier,
    authkit_jwks_url,
    remote_jwks_resolver,
)
from agentpit.auth.dependencies import make_current_user_dep
from agentpit.auth.google import GoogleTokenVerifier
from agentpit.auth.jwt import JwtCoder
from agentpit.auth.mcp_tokens import AgentVerifier
from agentpit.auth.workos_client import build_workos_client
from agentpit.config import Settings
from agentpit.db.session import DbSession
from agentpit.db.table_create import TableCreate
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.onchain.admin import OnchainAdmin
from agentpit.onchain.contracts import Contracts
from agentpit.onchain.deployment import ANVIL_CHAIN_ID, Deployment, is_disposable_chain
from agentpit.onchain.web3_client import Web3Client
from agentpit.liquidity import feed
from agentpit.liquidity.house_accounts import HouseAccountProvisioner
from agentpit.liquidity.mirror import MirrorEngine
from agentpit.liquidity.supervise import supervise
from agentpit.polymarket.polymarket_sync import (
    POLYMARKET_GAMMA_URL,
    CoveragePolicy,
    attach_teams,
    walk_admissions,
)
from agentpit.services.account_service import AccountService
from agentpit.services.agent_accounts import AgentAccounts
from agentpit.services.agent_desk import AgentDesk
from agentpit.services.auth_service import AuthService
from agentpit.services.event_service import EventService
from agentpit.services.leaderboard_service import LeaderboardService, drain
from agentpit.services.market_service import ChainTask, sweep
from agentpit.services.order_service import OrderService

log = logging.getLogger(__name__)

_LEADERBOARD_TICK_SECONDS = 2
_CATALOG_SECONDS = 30
_FULL_WALK_EVERY = 20


def _configure_root_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    root.handlers.clear()
    root.addHandler(handler)


async def _admin_gas_loop(admin: OnchainAdmin, settings: Settings) -> None:
    """Re-read the admin wallet for the gas breaker, and shout while it is low.

    The breaker (AdminTxSender) refuses sponsored sends below the stop
    level; this loop is what keeps its figure fresh and what a person
    reading the logs sees. There is no alerting beyond the logs.
    """
    while True:
        try:
            balance, state = await asyncio.to_thread(admin.refresh_admin_gas)
            if state in ("low", "paused"):
                log.error(
                    "ADMIN GAS %s: the admin wallet holds %.6f native (alarm %d gas, stop %d gas); %s",
                    state.upper(), balance / 1e18,
                    settings.admin_gas_alarm_gas, settings.admin_gas_stop_gas,
                    "sponsored sends are REFUSED" if state == "paused" else "refill it soon",
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("admin gas check failed")
        await asyncio.sleep(settings.admin_gas_check_interval_seconds)


def _start_admin_gas_loop(
    admin: OnchainAdmin, settings: Settings
) -> asyncio.Task | None:
    """Start `_admin_gas_loop`, or say loudly that it is off.

    An interval of 0 disables the loop, and the loop is the only thing that
    ever reads the admin balance: without it the balance stays unknown, and
    unknown means allowed, so a configured stop level would never fire. That is
    a breaker silently off, which is worse than one the operator chose to turn
    off, so it is a WARNING.
    """
    if settings.admin_gas_check_interval_seconds > 0:
        return asyncio.create_task(_admin_gas_loop(admin, settings))
    if settings.admin_gas_stop_gas > 0:
        log.warning(
            "Admin gas loop is OFF (AGENTPIT_ADMIN_GAS_CHECK_INTERVAL_SECONDS=0) but "
            "the stop level is %d gas: the admin balance is never read, so the "
            "breaker can never trip and sponsored sends are NEVER refused. Set the "
            "interval above 0, or AGENTPIT_ADMIN_GAS_STOP_GAS=0 to turn the breaker "
            "off on purpose.",
            settings.admin_gas_stop_gas,
        )
    return None


def _carried(db: DbSession) -> set[str]:
    with db.read() as conn:
        return TableRead.polymarket_condition_ids(conn)


async def _catalog_loop(
    db: DbSession, policy: CoveragePolicy, chain: ChainTask
) -> None:
    with httpx.Client(base_url=POLYMARKET_GAMMA_URL, timeout=20) as gamma:
        for tick in itertools.count():
            try:
                carried = (
                    set()
                    if tick % _FULL_WALK_EVERY == 0
                    else await asyncio.to_thread(_carried, db)
                )
                chain.admit(
                    await asyncio.to_thread(walk_admissions, gamma, policy, carried)
                )
            except Exception:
                log.exception("Admission walk failed")
            try:
                await asyncio.to_thread(attach_teams, db, gamma)
            except Exception:
                log.exception("Team fetch failed")
            try:
                await asyncio.to_thread(sweep, db, gamma, chain.resolve)
            except Exception:
                log.exception("Sweep failed")
            await asyncio.sleep(_CATALOG_SECONDS)


def _run_order_cleanup(db: DbSession, settings: Settings) -> None:
    now = int(time.time())
    with db.write() as conn:
        TableWrite.expire_due_orders(conn, now)
        TableWrite.purge_idempotency_keys(
            conn, now - settings.idempotency_key_retention_seconds
        )
        TableWrite.purge_mirrored_trades(conn, now - 30 * 86_400, limit=10_000)


async def _order_cleanup_loop(db: DbSession, settings: Settings) -> None:
    while True:
        try:
            await asyncio.to_thread(_run_order_cleanup, db, settings)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Order cleanup failed")
        await asyncio.sleep(settings.order_cleanup_interval_seconds)


def _run_leaderboard_tick(service) -> tuple[int, int]:
    now = int(time.time())
    written = service.take_snapshot(now)
    deleted = service.thin_snapshots(now)
    return written, deleted


async def _leaderboard_loop(service: LeaderboardService, interval_seconds: int) -> None:
    next_pass = 0.0
    while True:
        try:
            touched = drain()
            if touched:
                await asyncio.to_thread(service.take_snapshot, int(time.time()), touched)
            if time.monotonic() >= next_pass:
                next_pass = time.monotonic() + interval_seconds
                written, deleted = await asyncio.to_thread(_run_leaderboard_tick, service)
                log.info(
                    "Leaderboard tick: %d accounts valued, %d snapshots thinned",
                    written,
                    deleted,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Leaderboard tick failed")
        await asyncio.sleep(_LEADERBOARD_TICK_SECONDS)


def _warn_if_simulated_on_durable_chain(settings: Settings, chain_id: int) -> None:
    """AGENTPIT_SIMULATED_CHAIN=true outside anvil is ignored (see
    `is_disposable_chain`), but it is still a wrong config worth a loud line."""
    if settings.simulated_chain and not is_disposable_chain(chain_id):
        log.error(
            "AGENTPIT_SIMULATED_CHAIN=true is IGNORED on chain %d: re-running "
            "onboarding on login is only for a disposable anvil (%d). Set it to false.",
            chain_id, ANVIL_CHAIN_ID,
        )


def _build_onchain_admin(settings: Settings) -> OnchainAdmin:
    if not settings.deployment_path.exists():
        raise RuntimeError(
            f"on-chain deployment file {settings.deployment_path} not found — "
            "run scripts/run_node.sh && scripts/deploy_exchange.sh first"
        )
    deployment = Deployment.load(settings.deployment_path)
    client = Web3Client(settings, deployment)
    client.verify_chain()
    _warn_if_simulated_on_durable_chain(settings, deployment.chain_id)
    contracts = Contracts(client.web3, deployment)
    log.info(
        "on-chain stack ready: usd=%s faucet=%s exchange=%s",
        deployment.usd,
        deployment.faucet,
        deployment.exchange,
    )
    return OnchainAdmin(client, contracts)


def _claim_admin_key(database_url: str, admin_address: str) -> psycopg.Connection:
    conn = psycopg.connect(database_url, autocommit=True)
    if conn.execute(
        "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))", (admin_address,)
    ).fetchone() != (True,):
        conn.close()
        raise RuntimeError(
            f"admin key {admin_address} is held by another AgentPit API on this "
            "database; stop that one first"
        )
    return conn


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    _configure_root_logging()

    if settings.admin_token == "dev-admin-token":
        log.warning(
            "admin_token is the unsafe default — set AGENTPIT_ADMIN_TOKEN before "
            "exposing /admin/* to the network"
        )

    db_session = DbSession(
        settings.database_url,
        min_size=settings.pool_min_size,
        max_idle=settings.pool_max_idle,
        create_tables=False,
    )
    coder = JwtCoder(settings)
    onchain_admin = _build_onchain_admin(settings)
    # One verifier per app, for the same reason as the Google one below: the
    # resolver wraps a PyJWKClient that caches the JWKS, and a per-request
    # instance would re-fetch it from api.workos.com on every request. Building
    # it opens no connection -- the fetch is lazy, on the first token seen.
    authkit_verifier = (
        AuthKitVerifier(
            client_id=settings.workos_client_id,
            key_resolver=remote_jwks_resolver(authkit_jwks_url(settings.workos_client_id)),
        )
        if settings.workos_client_id
        else None
    )
    current_user_fn = make_current_user_dep(authkit_verifier)
    # One verifier per app: it caches Google's signing keys, and a per-request
    # instance would re-fetch them on every sign-in.
    google_verifier = (
        GoogleTokenVerifier(settings.google_client_id)
        if settings.google_client_id
        else None
    )
    if google_verifier is None:
        # Said out loud because the failure is otherwise invisible: with no
        # client id the button is absent and the endpoint 503s, which looks
        # exactly like a deploy that forgot the variable. It is one.
        log.info("Google sign-in disabled (set GOOGLE_CLIENT_ID to enable)")
    # One client per app, like the verifier above: it owns an httpx.Client with
    # its own connection pool, and a per-request instance would open a new TLS
    # connection to api.workos.com for every code mailed.
    workos_client = build_workos_client(settings)
    if workos_client is None or authkit_verifier is None:
        # log.error, not a raise. Since the cutover WorkOS is the ONLY way to
        # sign in, so this is not the minor gap the Google line above is -- but
        # raising here would stop the app serving `X-API-Key` traffic too, and
        # the trading bots authenticate with an api_key that has nothing to do
        # with WorkOS. Taking their `/order` down to protest a sign-in
        # misconfiguration would turn a bad deploy into a worse one.
        #
        # Compose cannot catch this for us: on the api side these arrive
        # through `env_file`, which has no `:?` form. The UI half IS gated, in
        # deploy/docker-compose.prod.yml.
        log.error(
            "WorkOS is not configured (WORKOS_API_KEY / WORKOS_CLIENT_ID): "
            "NOBODY CAN SIGN IN -- /auth/code and /auth/session 503 and AuthKit "
            "sessions are rejected. X-API-Key traffic is unaffected."
        )

    mcp_endpoint: McpEndpoint | None = None
    if workos_client is not None and settings.workos_authkit_domain.startswith("https://"):
        try:
            mcp_url = urlsplit(settings.mcp_url)
            if mcp_url.netloc and mcp_url.path.strip("/"):
                accounts = AgentAccounts(
                    db_session,
                    AuthService(db_session, coder, onchain_admin, settings)._onboard_new_account,
                )
                verifier = AgentVerifier(
                    issuer=settings.workos_authkit_domain,
                    resource=settings.mcp_url,
                    resolve=remote_jwks_resolver(f"{settings.workos_authkit_domain}/oauth2/jwks"),
                    workos=workos_client,
                    accounts=accounts,
                    db=db_session,
                )
                desk = AgentDesk(db_session, onchain_admin, settings)
                mcp_endpoint = McpEndpoint(settings, verifier, accounts, desk)
        except ValueError:
            pass
    if mcp_endpoint is None:
        log.error(
            "/mcp is off: it needs WORKOS_API_KEY, WORKOS_CLIENT_ID, an https "
            "WORKOS_AUTHKIT_DOMAIN and an http(s) AGENTPIT_MCP_URL with a host "
            "and a path. The REST API is unaffected."
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        admin_key = _claim_admin_key(
            settings.database_url, onchain_admin.oracle_address
        )
        with db_session.write() as conn:
            TableCreate.create_all_tables(conn)
            TableCreate.migrate_markets(conn, onchain_admin.oracle_address)
        try:
            wrapped = EventService(db_session).ensure_singleton_events_for_orphans()
            if wrapped:
                log.info("Wrapped %d orphan market(s) in singleton events", wrapped)
        except Exception:
            log.exception("orphan-market auto-wrap failed at startup")

        sync_tasks: list[asyncio.Task] = []
        if settings.sync_enabled:
            policy = CoveragePolicy.from_settings(settings)
            log.info("Polymarket sync enabled: %s", policy)
            chain = ChainTask(db_session, onchain_admin, settings)
            sync_tasks = [
                asyncio.create_task(
                    supervise(
                        "catalog", lambda: _catalog_loop(db_session, policy, chain)
                    )
                ),
                asyncio.create_task(supervise("chain", chain.run)),
                asyncio.create_task(supervise("redeem", chain.run_redeem)),
            ]
        else:
            log.info("Polymarket sync disabled (set SYNC=true to enable)")

        leaderboard_task: asyncio.Task | None = None
        if settings.leaderboard_enabled:
            log.info(
                "Leaderboard loop enabled (interval=%ds)",
                settings.leaderboard_interval_seconds,
            )
            leaderboard_service = LeaderboardService(
                db_session,
                onchain_admin,
                AccountService(
                    db_session, onchain_admin, min_claim_micro=settings.min_claim_micro
                ),
                settings,
            )
            leaderboard_task = asyncio.create_task(
                _leaderboard_loop(
                    leaderboard_service,
                    settings.leaderboard_interval_seconds,
                )
            )
        else:
            log.warning(
                "Leaderboard loop disabled (set AGENTPIT_LEADERBOARD_ENABLED=true "
                "to enable) -- the public board will stay permanently empty, and "
                "an empty board looks identical to one nobody has traded on yet"
            )

        mirror_tasks: list[asyncio.Task] = []
        if settings.liquidity_engine_enabled:
            log.info("Liquidity engine enabled")
            house = await asyncio.to_thread(
                HouseAccountProvisioner(
                    db_session, onchain_admin, settings
                ).ensure_provisioned
            )
            with db_session.write() as conn:
                dropped = TableWrite.delete_house_orders(conn, house.api_key)
            if dropped:
                log.info("Deleted %d unmatched house order rows", dropped)
            mirror_engine = MirrorEngine(
                db_session, settings, OrderService(db_session, onchain_admin, settings)
            )
            feed.HOUSE = feed.House(house, mirror_engine.state)
            # Supervised, not fire-and-forget: on 2026-09-02 the bare feed
            # task stopped and no new market was mirrored for 16 days (543 on
            # empty books). The staleness probe is what catches a hang, which
            # restart-on-exit alone cannot. Cancelling a supervisor cancels
            # and awaits its child, so the shutdown loop below still works.
            mirror_tasks = [
                asyncio.create_task(
                    supervise(
                        "mirror feed",
                        mirror_engine.run_feed,
                        is_stale=mirror_engine.feed_is_stale,
                    )
                ),
                asyncio.create_task(supervise("mirror house", mirror_engine.run_house)),
                asyncio.create_task(supervise("mirror tape", mirror_engine.run_tape)),
            ]
        else:
            log.info("Liquidity mirror disabled (set LIQUIDITY_ENGINE=true to enable)")

        log.info(
            "Order cleanup enabled (every %.0fs): expires due GTD orders",
            settings.order_cleanup_interval_seconds,
        )
        order_cleanup_task: asyncio.Task | None = asyncio.create_task(
            _order_cleanup_loop(db_session, settings)
        )
        live_task = asyncio.create_task(supervise("live", live.run))

        # Started after house provisioning on purpose: provisioning is a run of
        # sponsored sends, and a first refresh that finds the admin low must
        # not be what makes startup refuse them.
        admin_gas_task = _start_admin_gas_loop(onchain_admin, settings)

        try:
            async with mcp_endpoint.running() if mcp_endpoint else nullcontext():
                yield
        finally:
            for task in (
                leaderboard_task,
                order_cleanup_task,
                live_task,
                admin_gas_task,
                *sync_tasks,
                *mirror_tasks,
            ):
                if task is None:
                    continue
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            feed.HOUSE = None
            admin_key.close()
            # The pool is intentionally NOT closed here: a psycopg pool is
            # terminal once closed, and tests reuse the singleton app across
            # many TestClient lifespans. The pool is released by the test
            # harness (conftest session registry) or on process exit.

    app = FastAPI(title="AgentPit", lifespan=lifespan)
    app.dependency_overrides[get_db_session] = lambda: db_session
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_jwt_coder] = lambda: coder
    app.dependency_overrides[get_onchain_admin] = lambda: onchain_admin
    app.dependency_overrides[get_google_verifier] = lambda: google_verifier
    app.dependency_overrides[get_workos_client] = lambda: workos_client
    app.dependency_overrides[get_current_user] = current_user_fn

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)

    app.include_router(admin.router)
    app.include_router(system.router)
    app.include_router(auth.router)
    app.include_router(users.router)
    app.include_router(markets.router)
    app.include_router(events.router)
    app.include_router(tags.router)
    app.include_router(leaderboard.router)
    app.include_router(live.router)
    app.include_router(orders.router)
    app.include_router(market_data.router)
    app.include_router(positions.router)
    app.include_router(usdc.router)
    app.include_router(personalities.router)
    app.include_router(agents.router)
    app.include_router(data_api.router)
    if mcp_endpoint:
        app.router.routes.extend(mcp_endpoint.routes)

    return app
