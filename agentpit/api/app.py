import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager

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
from agentpit.api.routes import (
    admin,
    agents,
    auth,
    data_api,
    events,
    leaderboard,
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
from agentpit.auth.authkit_tokens import AuthKitVerifier, remote_jwks_resolver
from agentpit.auth.dependencies import make_current_user_dep
from agentpit.auth.google import GoogleTokenVerifier
from agentpit.auth.jwt import JwtCoder
from agentpit.auth.workos_client import build_workos_client
from agentpit.config import Settings
from agentpit.db.session import DbSession
from agentpit.db.table_write import TableWrite
from agentpit.onchain.admin import OnchainAdmin
from agentpit.onchain.contracts import Contracts
from agentpit.onchain.deployment import Deployment
from agentpit.onchain.web3_client import Web3Client
from agentpit.datastructures.user import User
from agentpit.liquidity.house_accounts import HouseAccountProvisioner, email_for
from agentpit.liquidity.mirror import MirrorEngine
from agentpit.liquidity.supervise import supervise
from agentpit.polymarket.pinned import (
    current_window_market_ids,
    ended_unresolved_window_ids,
    next_wake_delay,
    sync_pinned_series,
)
from agentpit.polymarket.polymarket_sync import (
    auto_redeem_resolved_markets,
    fetch_and_sync_polymarket_markets,
    list_scan_candidates,
    mirror_polymarket_resolutions,
)
from agentpit.services.account_service import AccountService
from agentpit.services.event_service import EventService
from agentpit.services.leaderboard_service import LeaderboardService
from agentpit.services.snapshot_service import SnapshotService

log = logging.getLogger(__name__)

# The full resolution loop and the fast pin-resolve loop both run auto-redeem
# off-thread; serialize them so they can't redeem the same position concurrently
# (a race that double-logged the payout — phantom collateral from the apUSD
# delta — even though on-chain only one redeem actually transferred).
_redeem_lock = threading.Lock()


def _configure_root_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    root.handlers.clear()
    root.addHandler(handler)


def _run_polymarket_sync(db: DbSession, admin: OnchainAdmin, settings: Settings) -> int:
    with db.write() as conn:
        created = fetch_and_sync_polymarket_markets(
            conn,
            admin,
            max_markets=settings.sync_max_markets,
            liquidity_min=settings.sync_liquidity_min,
            event_max_outcomes=settings.sync_event_max_outcomes,
            exclude_churn_series=settings.sync_exclude_churn_series,
            excluded_categories=settings.excluded_categories,
        )
    return len(created)


async def _polymarket_sync_loop(
    db: DbSession, admin: OnchainAdmin, settings: Settings
) -> None:
    while True:
        try:
            count = await asyncio.to_thread(_run_polymarket_sync, db, admin, settings)
            log.info("Polymarket sync added %d new markets", count)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Polymarket sync failed")
        await asyncio.sleep(settings.sync_interval_seconds)


def _run_resolution_cycle(
    db: DbSession, admin: OnchainAdmin, settings: Settings, scan_after: int = 0
) -> tuple[int, int, int]:
    """Returns (resolved, redeemed, next_scan_after).

    Two passes over different candidate sets. The first is the cheap one that
    always ran: markets whose stated end date has passed. The second walks the
    whole unsettled table a slice at a time, because Polymarket dates a match to
    the end of its tournament — 208 of 211 book-less ACTIVE markets on
    production were closed upstream with an end date still days away, so the
    first pass could not see them. `next_scan_after` wraps to 0 at the end.
    """
    now = int(time.time())
    with db.write() as conn:
        resolved = mirror_polymarket_resolutions(conn, admin, now=now)

        slice_ = list_scan_candidates(
            conn, after_market_id=scan_after, limit=settings.resolution_scan_batch
        )
        if slice_:
            resolved += mirror_polymarket_resolutions(
                conn, admin, now=now, candidates=slice_
            )
            scan_after = slice_[-1].market_id
        else:
            scan_after = 0
    redeemed = 0
    if settings.auto_redeem_enabled:
        with _redeem_lock:
            redeemed = auto_redeem_resolved_markets(db, admin)
    return resolved, redeemed, scan_after


async def _resolution_mirror_loop(
    db: DbSession, admin: OnchainAdmin, settings: Settings
) -> None:
    scan_after = 0
    while True:
        try:
            resolved, redeemed, scan_after = await asyncio.to_thread(
                _run_resolution_cycle, db, admin, settings, scan_after
            )
            log.info(
                "Resolution cycle: %d resolved, %d redeemed (scan past id %d)",
                resolved,
                redeemed,
                scan_after,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Resolution cycle failed")
        await asyncio.sleep(settings.resolution_mirror_interval_seconds)


async def _house_gas_loop(
    provisioner: HouseAccountProvisioner, users: list[User], settings: Settings
) -> None:
    """Keep the house accounts above the gas floor for as long as the API runs.

    Provisioning only happens at startup, so before this loop existed the house
    had exactly one grant per process and no way to earn more: it drained in
    ~82 minutes and every inventory split after that failed silently.
    """
    while True:
        try:
            funded = await asyncio.to_thread(provisioner.top_up_gas, users)
            if funded:
                log.info("House gas: %d account(s) topped up", funded)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("House gas top-up cycle failed")
        await asyncio.sleep(settings.liquidity_gas_check_interval_seconds)


def _run_pin_sync(db: DbSession, admin: OnchainAdmin, settings: Settings) -> list[int]:
    """Sync the current window of each pinned series; return the live-window
    market ids (created or pre-existing) for an immediate liquidity fill."""
    now = int(time.time())
    with db.write() as conn:
        sync_pinned_series(conn, admin, settings.pinned_series, now)
    with db.read() as conn:
        return current_window_market_ids(conn, settings.pinned_series, now)


async def _pin_sync_loop(
    db: DbSession,
    admin: OnchainAdmin,
    settings: Settings,
    mirror: "MirrorEngine | None",
) -> None:
    # Align to the smallest configured interval; wake `offset` seconds after
    # each boundary so the now-live window is synced promptly.
    align = min(interval for _, interval in settings.pinned_series)
    while True:
        try:
            ids = await asyncio.to_thread(_run_pin_sync, db, admin, settings)
            log.info("Pin-sync: %d current window(s)", len(ids))
            # Couple sync -> fill: a rotating window is live only ~5 min, far
            # too short for the mirror's discovery loop, so fill it immediately
            # (off-thread, so the API stays responsive).
            if mirror is not None and ids:
                placed = await mirror.fill_markets(ids)
                log.info("Pin-sync: filled %d order(s) on current windows", placed)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Pin-sync failed")
        await asyncio.sleep(
            next_wake_delay(int(time.time()), align, settings.pin_sync_offset_seconds)
        )


def _current_window_ids(db: DbSession, settings: Settings) -> list[int]:
    now = int(time.time())
    with db.read() as conn:
        return current_window_market_ids(conn, settings.pinned_series, now)


async def _pin_requote_once(
    db: DbSession, settings: Settings, mirror: "MirrorEngine"
) -> int:
    """Re-mirror the current live window(s) from the upstream book once.

    Returns the number of orders (re)placed. Kept self-contained so the loop
    body is unit-testable with a fake mirror.
    """
    ids = await asyncio.to_thread(_current_window_ids, db, settings)
    if not ids:
        return 0
    return await mirror.fill_markets(ids)


async def _pin_requote_loop(
    db: DbSession, settings: Settings, mirror: "MirrorEngine"
) -> None:
    # A rotating window lives only ~5 min — too short for the shared reconciler
    # to revisit. This fast loop re-pulls the upstream book for just the live
    # window(s) every few seconds so their local book tracks Polymarket.
    while True:
        try:
            await _pin_requote_once(db, settings, mirror)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Pin re-quote failed")
        await asyncio.sleep(settings.pin_requote_seconds)


def _run_pin_resolve(
    db: DbSession, admin: OnchainAdmin, settings: Settings
) -> tuple[int, int]:
    """Resolve + redeem just-ended pinned windows. Resolution is scoped to the
    few recently-ended windows (cheap — only those hit upstream); redeem runs
    against all resolved-unredeemed markets (a no-op query when there's nothing,
    so a redeem that failed last pass is retried promptly)."""
    now = int(time.time())
    with db.write() as conn:
        ids = ended_unresolved_window_ids(conn, settings.pinned_series, now)
        resolved = (
            mirror_polymarket_resolutions(conn, admin, now=now, market_ids=set(ids))
            if ids
            else 0
        )
    redeemed = 0
    if settings.auto_redeem_enabled:
        with _redeem_lock:
            redeemed = auto_redeem_resolved_markets(db, admin)
    return resolved, redeemed


async def _pin_resolve_loop(
    db: DbSession, admin: OnchainAdmin, settings: Settings
) -> None:
    # A 5-min window's winner should be paid within seconds of the upstream
    # market closing (which lags the local window end by a few minutes), not on
    # the slow full-scan resolution cycle — so poll the recently-ended windows.
    while True:
        try:
            resolved, redeemed = await asyncio.to_thread(
                _run_pin_resolve, db, admin, settings
            )
            if resolved or redeemed:
                log.info("Pin-resolve: %d resolved, %d redeemed", resolved, redeemed)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Pin-resolve failed")
        await asyncio.sleep(settings.pin_resolve_seconds)


def _run_order_cleanup(db: DbSession, settings: Settings) -> int:
    now = int(time.time())
    with db.write() as conn:
        TableWrite.expire_due_orders(conn, now)
        purged = TableWrite.purge_cancelled_orders(
            conn, now - settings.order_cancelled_retention_seconds
        )
        TableWrite.purge_idempotency_keys(
            conn, now - settings.idempotency_key_retention_seconds
        )
    return purged


async def _order_cleanup_loop(db: DbSession, settings: Settings) -> None:
    # Fast re-quoting leaves dead 'cancelled' rows behind; purge old ones so the
    # orders table doesn't grow without bound (the live queries stay fast either
    # way via the partial index).
    while True:
        try:
            purged = await asyncio.to_thread(_run_order_cleanup, db, settings)
            if purged:
                log.info("Order cleanup: purged %d old cancelled orders", purged)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Order cleanup failed")
        await asyncio.sleep(settings.order_cleanup_interval_seconds)


def _run_snapshot_tick(db: DbSession, retention_seconds: int) -> tuple[int, int]:
    service = SnapshotService(db)
    inserted = service.take_snapshot()
    deleted = service.prune_old(retention_seconds)
    return inserted, deleted


async def _snapshot_loop(
    db: DbSession, interval_seconds: int, retention_seconds: int
) -> None:
    while True:
        try:
            inserted, deleted = await asyncio.to_thread(
                _run_snapshot_tick, db, retention_seconds
            )
            log.info("Snapshot tick: %d inserted, %d pruned", inserted, deleted)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Snapshot tick failed")
        await asyncio.sleep(interval_seconds)


def _run_leaderboard_tick(service, retention_seconds: int) -> tuple[int, int]:
    now = int(time.time())
    written = service.take_snapshot(now)
    deleted = service.prune_old(now - retention_seconds)
    return written, deleted


async def _leaderboard_loop(
    service: LeaderboardService, interval_seconds: int, retention_seconds: int
) -> None:
    while True:
        try:
            written, deleted = await asyncio.to_thread(
                _run_leaderboard_tick, service, retention_seconds
            )
            log.info(
                "Leaderboard tick: %d accounts valued, %d snapshots pruned",
                written,
                deleted,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Leaderboard tick failed")
        await asyncio.sleep(interval_seconds)


def _build_onchain_admin(settings: Settings) -> OnchainAdmin:
    if not settings.deployment_path.exists():
        raise RuntimeError(
            f"on-chain deployment file {settings.deployment_path} not found — "
            "run scripts/run_node.sh && scripts/deploy_exchange.sh first"
        )
    deployment = Deployment.load(settings.deployment_path)
    client = Web3Client(settings, deployment)
    client.verify_chain()
    contracts = Contracts(client.web3, deployment)
    log.info(
        "on-chain stack ready: usd=%s faucet=%s exchange=%s",
        deployment.usd,
        deployment.faucet,
        deployment.exchange,
    )
    return OnchainAdmin(client, contracts)


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
            key_resolver=remote_jwks_resolver(settings.workos_client_id),
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
            "NOBODY CAN SIGN IN -- /auth/code and /auth/session 503, AuthKit "
            "sessions are rejected, and private-key export is disabled. "
            "X-API-Key traffic is unaffected."
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            wrapped = EventService(db_session).ensure_singleton_events_for_orphans()
            if wrapped:
                log.info("Wrapped %d orphan market(s) in singleton events", wrapped)
        except Exception:
            log.exception("orphan-market auto-wrap failed at startup")

        sync_task: asyncio.Task | None = None
        if settings.sync_enabled:
            log.info(
                "Polymarket sync enabled (interval=%ds)", settings.sync_interval_seconds
            )
            sync_task = asyncio.create_task(
                _polymarket_sync_loop(db_session, onchain_admin, settings)
            )
        else:
            log.info("Polymarket sync disabled (set SYNC=true to enable)")

        snapshot_task: asyncio.Task | None = None
        if settings.snapshot_enabled:
            retention_seconds = settings.snapshot_retention_days * 86_400
            log.info(
                "Snapshot loop enabled (interval=%ds, retention=%dd)",
                settings.snapshot_interval_seconds,
                settings.snapshot_retention_days,
            )
            snapshot_task = asyncio.create_task(
                _snapshot_loop(
                    db_session,
                    settings.snapshot_interval_seconds,
                    retention_seconds,
                )
            )
        else:
            log.info(
                "Snapshot loop disabled (set SNAPSHOT_ENABLED=true to enable)"
            )

        leaderboard_task: asyncio.Task | None = None
        if settings.leaderboard_enabled:
            log.info(
                "Leaderboard loop enabled (interval=%ds, retention=%dd)",
                settings.leaderboard_interval_seconds,
                settings.snapshot_retention_days,
            )
            leaderboard_service = LeaderboardService(
                db_session,
                onchain_admin,
                AccountService(db_session, onchain_admin),
                settings,
            )
            leaderboard_task = asyncio.create_task(
                _leaderboard_loop(
                    leaderboard_service,
                    settings.leaderboard_interval_seconds,
                    settings.snapshot_retention_days * 86_400,
                )
            )
        else:
            log.warning(
                "Leaderboard loop disabled (set AGENTPIT_LEADERBOARD_ENABLED=true "
                "to enable) -- the public board will stay permanently empty, and "
                "an empty board looks identical to one nobody has traded on yet"
            )

        mirror_tasks: list[asyncio.Task] = []
        mirror_engine: MirrorEngine | None = None
        if settings.liquidity_engine_enabled:
            log.info("Liquidity mirror enabled (reconcile interval=%ss)",
                     settings.mirror_reconcile_min_interval_seconds)
            provisioner = HouseAccountProvisioner(db_session, onchain_admin, settings)
            house_users = await asyncio.to_thread(provisioner.ensure_provisioned)
            mirror_user = next(
                (u for u in house_users if u.email == email_for(0)), house_users[0])
            mirror_engine = MirrorEngine(
                db_session, onchain_admin, settings, mirror_user)
            # Supervised, not fire-and-forget: on 2026-09-02 the bare feed
            # task stopped and no new market was mirrored for 16 days (543 on
            # empty books). The staleness probe is what catches a hang, which
            # restart-on-exit alone cannot. Cancelling a supervisor cancels
            # and awaits its child, so the shutdown loop below still works.
            mirror_tasks = [
                asyncio.create_task(supervise(
                    "mirror feed", mirror_engine.run_feed,
                    is_stale=mirror_engine.feed_is_stale)),
                asyncio.create_task(supervise(
                    "mirror reconciler", mirror_engine.run_reconciler)),
                asyncio.create_task(
                    _house_gas_loop(provisioner, house_users, settings)
                ),
            ]
            log.info(
                "House gas loop enabled (every %ss, floor %.1f ETH, target %.1f ETH)",
                settings.liquidity_gas_check_interval_seconds,
                settings.liquidity_gas_floor_wei / 1e18,
                settings.liquidity_gas_target_wei / 1e18,
            )
        else:
            log.info("Liquidity mirror disabled (set LIQUIDITY_ENGINE=true to enable)")

        resolution_task: asyncio.Task | None = None
        if settings.resolution_mirror_enabled:
            log.info(
                "Resolution/redeem loop enabled (interval=%ds, auto_redeem=%s)",
                settings.resolution_mirror_interval_seconds,
                settings.auto_redeem_enabled,
            )
            resolution_task = asyncio.create_task(
                _resolution_mirror_loop(db_session, onchain_admin, settings)
            )
        else:
            log.info(
                "Resolution/redeem loop disabled "
                "(set RESOLUTION_MIRROR_ENABLED=true to enable)"
            )

        pin_task: asyncio.Task | None = None
        if settings.pin_sync_enabled and settings.pinned_series:
            align = min(i for _, i in settings.pinned_series)
            log.info(
                "Pin-sync enabled (%d series, align=%ds, offset=%ds)",
                len(settings.pinned_series),
                align,
                settings.pin_sync_offset_seconds,
            )
            pin_task = asyncio.create_task(
                _pin_sync_loop(db_session, onchain_admin, settings, mirror_engine)
            )
        else:
            log.info(
                "Pin-sync disabled (set PIN_SYNC_ENABLED=true/SYNC and PINNED_SERIES)"
            )

        requote_task: asyncio.Task | None = None
        if (
            mirror_engine is not None
            and settings.pin_sync_enabled
            and settings.pinned_series
        ):
            log.info(
                "Pin re-quote enabled (every %.1fs) — live windows track upstream",
                settings.pin_requote_seconds,
            )
            requote_task = asyncio.create_task(
                _pin_requote_loop(db_session, settings, mirror_engine)
            )

        pin_resolve_task: asyncio.Task | None = None
        if settings.pin_sync_enabled and settings.pinned_series:
            log.info(
                "Pin-resolve enabled (every %.0fs) — fast payout for ended windows",
                settings.pin_resolve_seconds,
            )
            pin_resolve_task = asyncio.create_task(
                _pin_resolve_loop(db_session, onchain_admin, settings)
            )

        # Independent of the liquidity engine on purpose: this loop does two
        # jobs. Purging old cancelled rows exists because the mirror's fast
        # re-quoting piles them up -- that part could reasonably wait on the
        # engine. But it also sweeps due GTD orders out of the live set, and
        # GTD expiry is a user-facing order type any client can place, engine
        # or no engine. Gating the whole loop on liquidity_engine_enabled used
        # to mean an expired order stayed live forever -- present, still
        # tradable, and unreachable by purge_cancelled_orders -- on any
        # deployment that ran with the engine off.
        log.info(
            "Order cleanup enabled (every %.0fs) -- expires due GTD orders and "
            "purges cancelled ones past retention=%ds",
            settings.order_cleanup_interval_seconds,
            settings.order_cancelled_retention_seconds,
        )
        order_cleanup_task: asyncio.Task | None = asyncio.create_task(
            _order_cleanup_loop(db_session, settings)
        )

        try:
            yield
        finally:
            for task in (
                sync_task,
                snapshot_task,
                leaderboard_task,
                resolution_task,
                pin_task,
                requote_task,
                pin_resolve_task,
                order_cleanup_task,
                *mirror_tasks,
            ):
                if task is None:
                    continue
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            # The pool is intentionally NOT closed here: a psycopg pool is
            # terminal once closed (unlike the old auto-reconnecting SQLite
            # session), and tests reuse the singleton app across many
            # TestClient lifespans. The pool is released by the test harness
            # (conftest session registry) or on process exit.

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
    app.include_router(orders.router)
    app.include_router(market_data.router)
    app.include_router(positions.router)
    app.include_router(usdc.router)
    app.include_router(personalities.router)
    app.include_router(agents.router)
    app.include_router(data_api.router)

    return app
