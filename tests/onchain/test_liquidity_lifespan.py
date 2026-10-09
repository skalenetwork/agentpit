# tests/onchain/test_liquidity_lifespan.py
import asyncio
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agentpit.api.app import create_app
from agentpit.config import Settings
from agentpit.db.table_create import TableCreate
from agentpit.liquidity import feed

from tests.onchain._helpers import ADMIN_HDR


def test_mirror_disabled_by_default():
    app = create_app(Settings())
    with TestClient(app):
        pass  # lifespan runs; no mirror tasks, no house provisioning, no crash


def test_one_api_per_admin_key():
    first, second = create_app(Settings()), create_app(Settings())
    with TestClient(first):
        with patch.object(TableCreate, "create_all_tables") as ddl:
            with pytest.raises(RuntimeError, match="held by another AgentPit API"):
                with TestClient(second):
                    pass
        assert ddl.call_count == 0
    with TestClient(second):
        pass


def test_mirror_enabled_spawns_and_cancels_cleanly(monkeypatch):
    import time
    import uuid

    calls = []

    async def fake_run(conn):
        calls.append(sorted(conn.assets))
        await asyncio.Event().wait()

    monkeypatch.setattr(feed.FeedConnection, "run", fake_run)

    s = Settings(liquidity_engine_enabled=True, mirror_target_refresh_seconds=0.1)
    app = create_app(s)
    with TestClient(app) as client:
        r = client.get("/markets")        # API serves while the mirror idles
        assert r.status_code == 200
        # Give the engine a synced market; the 0.1s target refresh must pick it
        # up and spawn a (stubbed) feed connection for its Polymarket asset.
        m = client.post("/markets", json={
            "question": f"LS {uuid.uuid4().hex[:6]}?", "description": "x",
            "outcome_labels": ["YES", "NO"]}, headers=ADMIN_HDR).json()
        from agentpit.db.session import DbSession
        db = DbSession(s.database_url)
        with db.write() as conn:
            conn.execute(
                "UPDATE markets SET MARKET_STATE='ACTIVE', "
                "POLYMARKET_CONDITION_ID=%s, POLYMARKET_YES_TOKEN_ID=%s "
                "WHERE CONDITION_ID=%s",
                ("0xpm-ls", "PM-LS", m["condition_id"]["value"]))
        deadline = time.time() + 5.0
        while time.time() < deadline and not calls:
            time.sleep(0.05)
        assert calls and calls[0] == ["PM-LS"], \
            "target refresh must spawn a feed connection for the synced market"
        assert feed.HOUSE is not None and feed.HOUSE.user.is_bot
    assert feed.HOUSE is None
    # Clean shutdown (no hang, no unraised CancelledError) is the assertion.
