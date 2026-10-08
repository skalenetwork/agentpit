import os
import re
from pathlib import Path

import psycopg
from psycopg import sql

_REPO_ROOT = Path(__file__).resolve().parent.parent
# Comment put on a database this file created for a worktree, followed by the
# worktree's path. The sweep below drops only databases carrying it, never one
# somebody made by hand.
_WORKTREE_DB_LABEL = "agentpit test db for worktree "


def _worktree_test_db(admin: psycopg.Connection) -> str:
    """`agentpit_test_<worktree>`, created on first use. Every test TRUNCATEs
    every table first, so sessions in parallel worktrees sharing one database
    wipe each other's rows mid-run."""
    suffix = re.sub(r"[^a-z0-9_]", "_", _REPO_ROOT.name.lower())
    name = f"agentpit_test_{suffix}"[:63]  # Postgres's identifier limit
    found = admin.execute(
        "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
    ).fetchone()
    if not found:
        try:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        except psycopg.errors.DuplicateDatabase:
            pass  # a concurrent run in the same worktree created it first
    # On every run, so a database created before the label existed gets it too.
    admin.execute(
        sql.SQL("COMMENT ON DATABASE {} IS {}").format(
            sql.Identifier(name), sql.Literal(_WORKTREE_DB_LABEL + str(_REPO_ROOT))
        )
    )
    return name


def _drop_orphaned_worktree_dbs(admin: psycopg.Connection) -> None:
    """Removing a worktree leaves its database behind, so every run drops the
    labelled databases whose worktree is gone from disk."""
    rows = admin.execute(
        "SELECT datname, shobj_description(oid, 'pg_database') FROM pg_database "
        "WHERE shobj_description(oid, 'pg_database') LIKE %s",
        (_WORKTREE_DB_LABEL + "%",),
    ).fetchall()
    for name, label in rows:
        if Path(label.removeprefix(_WORKTREE_DB_LABEL)).exists():
            continue
        try:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))
        except psycopg.errors.ObjectInUse:
            pass  # something is still connected; a later run tries again


# Tests run against a real local Postgres (the suite already requires anvil +
# the deployed exchange — no off-mode). Each test gets a clean DB via TRUNCATE
# and its own DbSession pool, overridden onto the shared app. That DB is
# `agentpit_test`, except in a linked git worktree, which gets its own: a linked
# worktree's .git is a file pointing back at the main repo, while the main
# checkout (and CI's actions/checkout) has a .git directory.
with psycopg.connect("postgresql:///postgres", autocommit=True) as _admin:
    _drop_orphaned_worktree_dbs(_admin)
    if "AGENTPIT_DATABASE_URL" not in os.environ:
        _in_worktree = (_REPO_ROOT / ".git").is_file()
        _db = _worktree_test_db(_admin) if _in_worktree else "agentpit_test"
        os.environ["AGENTPIT_DATABASE_URL"] = f"postgresql:///{_db}"
os.environ.setdefault("AGENTPIT_POOL_MIN_SIZE", "0")  # leaked create_app pools hold 0 conns
os.environ.setdefault("AGENTPIT_POOL_MAX_IDLE", "5")  # shed idle connections fast in tests
os.environ.setdefault("SYNC", "false")
# The liquidity engine provisions the single mirror house account at startup; never
# let a developer's .env (LIQUIDITY_ENGINE=true) leak in and do that per test.
os.environ.setdefault("LIQUIDITY_ENGINE", "false")
# The leaderboard timer walks every traded account's chain balance; keep it
# off the same way the other timers above are, so a developer's .env can't
# make every TestClient(app) lifespan start ticking it.
os.environ.setdefault("AGENTPIT_LEADERBOARD_ENABLED", "false")
os.environ.setdefault("JWT_SECRET", "test-only-secret")
# The $1 minimum order (AGENTPIT_MIN_ORDER_NOTIONAL_MICRO) would reject the
# small orders much of the suite places; guard tests set it explicitly.
os.environ.setdefault("AGENTPIT_MIN_ORDER_NOTIONAL_MICRO", "0")

import pytest

from agentpit.api.deps import (
    get_current_user,
    get_db_session,
    get_workos_client,
)
from agentpit.api.main import app
from agentpit.auth.authkit_tokens import AuthKitVerifier
from agentpit.auth.dependencies import make_current_user_dep
from agentpit.auth.workos_client import FakeWorkOsClient
from agentpit.db.session import DbSession
from agentpit.db.table_create import TableCreate
from tests.db_helpers import TEST_DSN, fresh_test_db

# Ensure the schema exists once up-front (before the first truncate).
_boot = psycopg.connect(TEST_DSN, autocommit=True)
TableCreate.create_all_tables(_boot)
_boot.close()

# The offline double is the DEFAULT WorkOS client for the shared app, not just
# something individual tests remember to install.
#
# `agentpit.api.main` calls load_dotenv() at import, and this repo's .env
# carries a live WORKOS_API_KEY, so the app factory wires a RealWorkOsClient
# pointed at https://api.workos.com. A test that posts /auth/code and forgets
# its double would then hit the real API with the production key -- and
# measured 2026-08-11, POST /user_management/magic_auth CREATES the WorkOS user
# and MAILS the code, so it would send third-party mail and pass, on every CI
# run, with nothing to notice it by. Offline by construction instead of by
# everyone remembering. One shared instance rather than a fresh one per
# request, because the fake holds the issued codes: a per-request instance
# would make a forgotten fixture fail as a confusing 401 instead of working.
_default_workos = FakeWorkOsClient()
app.dependency_overrides[get_workos_client] = lambda: _default_workos


def _no_live_jwks(_token: str):
    raise RuntimeError(
        "this test would have fetched the live WorkOS JWKS -- override "
        "get_current_user with a dependency built over a local key resolver, "
        "the way tests/auth/test_current_user_authkit.py does"
    )


# The other half of the same argument, for the other half of the AuthKit
# wiring: `create_app` builds an AuthKitVerifier whose resolver fetches
# https://api.workos.com/sso/jwks/<client_id>, and the .env that load_dotenv()
# pulls in above carries a real WORKOS_CLIENT_ID. A test that sends an
# AuthKit-shaped bearer token to the shared app would then reach out to WorkOS
# and merely look slow, rather than fail as the network-free rule it broke.
# This default cannot reach anything: its resolver raises. The X-API-Key path
# is byte for byte what create_app wired -- it is ahead of the bearer branch
# and touches no verifier at all.
_default_current_user = make_current_user_dep(
    AuthKitVerifier(client_id="client_test_offline", key_resolver=_no_live_jwks)
)
app.dependency_overrides[get_current_user] = _default_current_user


def _truncate_all() -> None:
    with psycopg.connect(TEST_DSN, autocommit=True) as c:
        tables = [
            r[0]
            for r in c.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'"
            ).fetchall()
        ]
        if tables:
            c.execute(
                "TRUNCATE " + ", ".join(tables) + " RESTART IDENTITY CASCADE"
            )


@pytest.fixture(autouse=True)
def _isolated_db_session():
    """Clean the shared test DB and give each test its own DbSession pool,
    overridden onto the singleton app (the app's lifespan closes its own
    db_session on TestClient shutdown, so endpoints must use the override)."""
    _truncate_all()
    # The /events listing has a per-process TTL cache; clear it so a previous
    # test's cached page can't leak into one that hits the same (limit, offset).
    from agentpit.api.routes import events as _events_route

    _events_route._events_cache.clear()
    # Same reason as the events cache above: /tags holds a single 30s slot, so
    # a response built from a previous test's rows would be served to the next.
    from agentpit.api.routes import tags as _tags_route

    _tags_route._tags_cache = None
    # Same reason as the events cache above: holdings and touched accounts
    # left by a previous test would leak into the next one.
    from agentpit.services import leaderboard_service as _leaderboard_service

    _leaderboard_service._holdings.clear()
    _leaderboard_service._dirty.clear()
    # Same reason again: a revert backoff left by a previous test would make
    # the next test's auto-redeem pass skip a holder it should claim for.
    from agentpit.polymarket import polymarket_sync as _polymarket_sync

    _polymarket_sync._revert_backoff_until.clear()
    before = {id(s) for s in DbSession._open}
    fresh = fresh_test_db()
    previous = app.dependency_overrides.get(get_db_session)
    app.dependency_overrides[get_db_session] = lambda: fresh
    try:
        yield
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_db_session, None)
        else:
            app.dependency_overrides[get_db_session] = previous
        # Close every session created during this test (fresh + any pools
        # leaked by per-test create_app()); keep the import-time singleton.
        for s in list(DbSession._open):
            if id(s) not in before:
                s.close()
