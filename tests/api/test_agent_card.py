import struct
from pathlib import Path
from typing import get_args

import httpx
from fastapi.testclient import TestClient

from agentpit.api import og_card
from agentpit.api.main import app
from agentpit.db.table_write import TableWrite
from agentpit.domain.runner import RunnerSlug
from tests.db_helpers import fresh_test_conn

ROBOT = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 120"><rect width="120" height="120" fill="#E07000"/></svg>'


def _landing(monkeypatch, response: httpx.Response) -> list[str]:
    requested: list[str] = []

    def serve(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return response

    monkeypatch.setattr(og_card, "_http", httpx.Client(transport=httpx.MockTransport(serve)))
    return requested


def _agent() -> str:
    conn = fresh_test_conn()
    _user_id, acct, _key = TableWrite.create_user(conn, email=None, password_hash=None, handle="IcyOasis", agent_app="Cursor")
    conn.close()
    return acct.address


def test_a_known_agent_gets_a_week_cached_png(monkeypatch):
    address = _agent()
    requested = _landing(monkeypatch, httpx.Response(200, content=ROBOT))
    with TestClient(app) as client:
        resp = client.get(f"/agents/{address.upper().replace('0X', '0x')}/card.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["cache-control"] == "public, max-age=604800"
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", resp.content[16:24]) == (1200, 630)
    assert requested == [f"https://agentpit.dev/agents/{address.lower()}/avatar.svg"]


def test_an_address_with_no_account_is_an_empty_404(monkeypatch):
    requested = _landing(monkeypatch, httpx.Response(200, content=ROBOT))
    with TestClient(app) as client:
        for address in ("0x" + "ab" * 20, "nonsense"):
            resp = client.get(f"/agents/{address}/card.png")
            assert resp.status_code == 404
            assert resp.content == b""
    assert requested == []


def test_a_landing_failure_is_an_uncached_503(monkeypatch):
    address = _agent()
    _landing(monkeypatch, httpx.Response(500))
    with TestClient(app) as client:
        resp = client.get(f"/agents/{address}/card.png")
    assert resp.status_code == 503
    assert resp.headers["cache-control"] == "no-store"


def test_every_runner_has_a_mark():
    marks = Path(og_card.ASSETS, "runners")
    assert {p.stem for p in marks.glob("*.svg")} == set(get_args(RunnerSlug))
