"""The anonymous by-address reads (/positions, /closed-positions, /activity,
/value) serve live accounts only: the house is the maker of every fill, so
its history is everyone's, and a deleted agent has left the board."""

import time

import pytest
from fastapi.testclient import TestClient

from agentpit.api.main import app
from agentpit.db.table_write import TableWrite
from agentpit.services.account_service import AccountService
from tests.db_helpers import fresh_test_conn


def _account(conn, handle: str) -> tuple[str, str, str]:
    user_id, acct, key = TableWrite.create_user(conn, email=None, password_hash=None, handle=handle)
    return user_id, acct.address, key


@pytest.fixture
def served(monkeypatch) -> list[str]:
    """Addresses the account reads were asked to build, in call order."""
    asked: list[str] = []

    def listed(self, address, *args, **kwargs):
        asked.append(address)
        return []

    def valued(self, address):
        asked.append(address)
        return [{"user": address, "value": 0}]

    for method in ("list_positions", "list_closed_positions", "list_activity"):
        monkeypatch.setattr(AccountService, method, listed)
    monkeypatch.setattr(AccountService, "total_value", valued)
    return asked


def _read_all(client: TestClient, address: str) -> list:
    return [
        client.get(path, params={"user": address}).json()
        for path in ("/positions", "/closed-positions", "/activity", "/value")
    ]


def test_the_public_reads_skip_the_house_bots_and_deleted_agents(served):
    conn = fresh_test_conn()
    _, live, _ = _account(conn, "Live")
    _, house, house_key = _account(conn, "House")
    TableWrite.mark_user_as_bot(conn, house_key)
    gone_id, gone, _ = _account(conn, "Gone")
    TableWrite.delete_agent(conn, gone_id, int(time.time()))
    conn.close()

    with TestClient(app) as client:
        refused = [_read_all(client, address) for address in (house, gone)]
        _read_all(client, live)

    assert refused == [[[], [], [], [{"user": a, "value": 0}]] for a in (house, gone)]
    assert served == [live] * 4


def test_an_activity_page_is_bounded_like_polymarket_s(monkeypatch):
    pages: list[tuple[int, int]] = []

    def listed(self, address, *, limit, offset, **kwargs):
        pages.append((limit, offset))
        return []

    monkeypatch.setattr(AccountService, "list_activity", listed)
    conn = fresh_test_conn()
    _, live, _ = _account(conn, "Pager")
    conn.close()
    with TestClient(app) as client:
        refused = [
            client.get("/activity", params={"user": live, **params}).status_code
            for params in ({"limit": -1}, {"offset": -1}, {"offset": 5001})
        ]
        served = [
            client.get("/activity", params={"user": live, **params}).status_code
            for params in ({"limit": 10_000}, {"limit": 500, "offset": 5000})
        ]
    assert (refused, served) == ([422, 422, 422], [200, 200])
    assert pages == [(500, 0), (500, 5000)]
