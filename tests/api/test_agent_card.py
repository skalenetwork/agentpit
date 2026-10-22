import struct
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import get_args

import httpx
from fastapi.testclient import TestClient

from agentpit.api import og_card
from agentpit.api.main import app
from agentpit.api.routes import agents as agents_route
from agentpit.db.table_write import TableWrite
from agentpit.domain.runner import RunnerSlug
from agentpit.services.leaderboard_service import Holdings, LeaderboardRow, _holdings
from tests.db_helpers import fresh_test_conn

G = 100_000_000_000
NOW = int(time.time())
ROBOT = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 120"><rect width="120" height="120" fill="#E07000"/></svg>'


def _landing(monkeypatch, response: httpx.Response) -> list[str]:
    requested: list[str] = []

    def serve(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return response

    monkeypatch.setattr(og_card, "_http", httpx.Client(transport=httpx.MockTransport(serve)))
    return requested


def _agent() -> str:
    """A traded agent on the board, whose valuation the pass has already kept."""
    conn = fresh_test_conn()
    user_id, acct, key = TableWrite.create_user(conn, email=None, password_hash=None, handle="IcyOasis", agent_app="Cursor")
    conn.execute("INSERT INTO trades (TRADE_ID, TAKER_API_KEY, MATCH_TIME, STATUS) VALUES ('t1', %s, %s, 'CONFIRMED')", (key, NOW - 60))
    TableWrite.insert_account_snapshot(conn, user_id, NOW - 30, G + G // 10, G)
    conn.close()
    _holdings[acct.address] = Holdings(valued_at=NOW - 30, cash_raw=0, positions=[], closed=[])
    return acct.address


def test_an_agent_on_the_board_gets_a_five_minute_png(monkeypatch):
    address = _agent()
    requested = _landing(monkeypatch, httpx.Response(200, content=ROBOT))
    with TestClient(app) as client:
        resp = client.get(f"/agents/{address.upper().replace('0X', '0x')}/card.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["cache-control"] == "public, max-age=300"
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", resp.content[16:24]) == (1200, 630)
    assert requested == [f"https://agentpit.dev/agents/{address.lower()}/avatar.svg"]


def test_the_card_draws_a_ranked_row_with_its_move_and_a_warming_row():
    ranked = LeaderboardRow(
        name="IcyOasis",
        address="0x" + "ab" * 20,
        app="Cursor",
        host=None,
        capital_raw=G + G // 10,
        deposited_raw=G,
        trades=12,
        first_trade_at=NOW - 60,
        last_trade_at=NOW - 60,
        trend=["0", "-4000000000", str(G // 10)],
        place=2,
        place_change=-3,
    )
    for row in (ranked, ranked.model_copy(update={"trades": 3, "place": None, "place_change": None})):
        png = og_card.render_card(row, 19, NOW, ROBOT)
        assert struct.unpack(">II", png[16:24]) == (1200, 630)


def test_an_agent_not_on_the_board_is_an_empty_404(monkeypatch):
    conn = fresh_test_conn()
    _user_id, idle, _key = TableWrite.create_user(conn, email=None, password_hash=None, handle="Idle", agent_app="Cursor")
    conn.close()
    requested = _landing(monkeypatch, httpx.Response(200, content=ROBOT))
    with TestClient(app) as client:
        for address in (idle.address, "0x" + "ab" * 20, "nonsense"):
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


def test_an_unchanged_card_is_drawn_once(monkeypatch):
    address = _agent()
    requested = _landing(monkeypatch, httpx.Response(200, content=ROBOT))
    with TestClient(app) as client:
        first = client.get(f"/agents/{address.lower()}/card.png")
        again = client.get(f"/agents/{address.upper().replace('0X', '0x')}/card.png")
    assert (first.status_code, again.status_code) == (200, 200)
    assert again.content == first.content
    assert len(requested) == 1


def _counted_renders(monkeypatch, pause: float = 0.0) -> list[int]:
    renders: list[int] = []
    real = agents_route.render_card

    def render(*args):
        renders.append(1)
        time.sleep(pause)
        return real(*args)

    monkeypatch.setattr(agents_route, "render_card", render)
    return renders


def test_a_card_is_drawn_again_once_its_row_changes_with_the_same_robot(monkeypatch):
    address = _agent()
    requested = _landing(monkeypatch, httpx.Response(200, content=ROBOT))
    renders = _counted_renders(monkeypatch)
    with TestClient(app) as client:
        client.get(f"/agents/{address}/card.png")
        _holdings[address] = Holdings(valued_at=NOW, cash_raw=0, positions=[], closed=[])
        client.get(f"/agents/{address}/card.png")
    assert (len(renders), len(requested)) == (2, 1)


def test_requests_for_the_same_card_at_once_share_one_draw(monkeypatch):
    address = _agent()
    _landing(monkeypatch, httpx.Response(200, content=ROBOT))
    renders = _counted_renders(monkeypatch, pause=0.2)
    with TestClient(app) as client, ThreadPoolExecutor(2) as pool:
        first, again = pool.map(lambda _: client.get(f"/agents/{address}/card.png"), range(2))
    assert (first.status_code, again.status_code, len(renders)) == (200, 200, 1)
    assert again.content == first.content


def test_a_drawn_card_is_served_while_another_is_drawn(monkeypatch):
    address = _agent()
    _landing(monkeypatch, httpx.Response(200, content=ROBOT))
    with TestClient(app) as client:
        first = client.get(f"/agents/{address}/card.png")
        assert og_card._render_gate.acquire(blocking=False)
        try:
            again = client.get(f"/agents/{address}/card.png")
        finally:
            og_card._render_gate.release()
    assert (again.status_code, again.content) == (200, first.content)


def test_a_card_that_cannot_start_its_draw_in_time_gets_a_503(monkeypatch):
    monkeypatch.setattr(og_card, "_RENDER_WAIT_S", 0.01)
    address = _agent()
    _landing(monkeypatch, httpx.Response(200, content=ROBOT))
    assert og_card._render_gate.acquire(blocking=False)
    try:
        with TestClient(app) as client:
            resp = client.get(f"/agents/{address}/card.png")
    finally:
        og_card._render_gate.release()
    assert resp.status_code == 503
    assert (resp.headers["cache-control"], resp.headers["retry-after"]) == ("no-store", "1")
    assert og_card._cards == {}


def test_a_failed_render_frees_the_gate(monkeypatch):
    address = _agent()
    _landing(monkeypatch, httpx.Response(200, content=ROBOT))

    def fail(*args):
        raise RuntimeError("render failed")

    monkeypatch.setattr(agents_route, "render_card", fail)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get(f"/agents/{address}/card.png").status_code == 500
    assert og_card._render_gate.acquire(blocking=False)
    og_card._render_gate.release()


def test_the_card_cache_keeps_at_most_its_cap(monkeypatch):
    monkeypatch.setattr(og_card, "_CARDS_MAX", 2)
    og_card._cards.update({"0xa": ("k", b""), "0xb": ("k", b"")})
    address = _agent()
    _landing(monkeypatch, httpx.Response(200, content=ROBOT))
    with TestClient(app) as client:
        assert client.get(f"/agents/{address}/card.png").status_code == 200
    assert list(og_card._cards) == ["0xb", address.lower()]
