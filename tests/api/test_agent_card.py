import struct
import time
from pathlib import Path
from typing import get_args

import httpx
from fastapi.testclient import TestClient

from agentpit.api import og_card
from agentpit.api.main import app
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
