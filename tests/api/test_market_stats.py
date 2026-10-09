import pytest
from fastapi.testclient import TestClient

from agentpit.api.deps import get_db_session
from agentpit.api.main import app
from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.market_state import MarketState
from agentpit.db.table_write import TableWrite


def _hex32(seed: str) -> str:
    return "0x" + seed.encode().hex().ljust(64, "0")[:64]


def _make(conn, seed: str, state: MarketState):
    return TableWrite.create_market(
        conn,
        CreateMarketRequest(
            question=f"Q {seed}?",
            description="d",
            erc1155_tokens=[(f"{seed}1", "Yes"), (f"{seed}2", "No")],
            slug=f"slug-{seed}",
            condition_id=ConditionId(_hex32(seed)),
            state=state,
        ),
        is_polygon_market=False,
    )


@pytest.fixture()
def client_with_mixed_states(house_book):
    session = app.dependency_overrides[get_db_session]()
    with session.write() as conn:
        _make(conn, "a", MarketState.ACTIVE)
        _make(conn, "b", MarketState.ACTIVE)
        _make(conn, "c", MarketState.CLOSED)
        _make(conn, "d", MarketState.DRAFT)
        _make(conn, "e", MarketState.CANCELLED)
        _make(conn, "f", MarketState.ACTIVE)
    for seed in "abcdf":
        house_book(
            f"{seed}1",
            bids=(("0.4", "1"),),
            asks=() if seed == "f" else (("0.6", "1"),),
        )
    with TestClient(app) as client:
        yield client


def test_stats_counts_only_open_markets(client_with_mixed_states):
    resp = client_with_mixed_states.get("/markets/stats")
    assert resp.status_code == 200
    assert resp.json() == {"active": 2}


def test_stats_route_is_not_shadowed_by_the_market_id_route(client_with_mixed_states):
    # /markets/{market_id} is typed int, so if "stats" registered below it the
    # path would match there and fail validation with 422 instead of resolving.
    resp = client_with_mixed_states.get("/markets/stats")
    assert resp.status_code != 422


def test_stats_is_platform_wide_not_page_sized(client_with_mixed_states):
    # The bug this endpoint exists to kill: a client summing a paged response
    # reports how far it has scrolled. One market per page must not change the
    # headline count.
    active = client_with_mixed_states.get("/markets/stats").json()["active"]
    page = client_with_mixed_states.get("/markets?limit=1&offset=0").json()
    assert len(page) == 1
    assert active == 2
