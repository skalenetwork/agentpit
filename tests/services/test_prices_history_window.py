"""A price holds until the next trade, so a window that contains no trade is
not a window with no price.

`/prices-history?interval=1d` filtered on `MATCH_TIME >= start` alone, so a
market whose last print fell just outside the window came back EMPTY — and the
card, which asks for exactly that window, drew its no-data placeholder next to a
perfectly good 19% headline. "Clarity Act (H.R.3633) signed into law in 2026?"
had 571 prints; the newest was 26 hours old.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.market_state import MarketState
from agentpit.db.table_write import TableWrite
from agentpit.liquidity import tape
from agentpit.services.order_service import OrderService
from tests.db_helpers import fresh_test_db

TOKEN = "77" + "0" * 20
COND = "0x" + "ab" * 32
HOUR = 3600


@pytest.fixture()
def svc() -> Any:
    db = fresh_test_db()
    with db.write() as conn:
        TableWrite.create_market(
            conn,
            CreateMarketRequest(
                question="Clarity Act signed into law?",
                description="d",
                erc1155_tokens=[(TOKEN, "Yes"), (TOKEN + "1", "No")],
                slug="clarity-act",
                condition_id=ConditionId(COND),
                state=MarketState.ACTIVE,
            ),
            is_polygon_market=False,
        )
    yield OrderService(db, None)  # get_prices_history never touches the chain
    db.close()


def _print_at(svc, *, seconds_ago: int, price_micro: int) -> None:
    with svc._db.write() as conn:
        tape.insert_mirrored_trades(
            conn,
            [(COND, TOKEN, TOKEN + "1", price_micro, 1_000_000, "BUY", int(time.time()) - seconds_ago)],
        )


def _history(svc, interval="1d"):
    return svc.get_prices_history(TOKEN, interval=interval)["history"]


def test_a_price_from_before_the_window_opens_it(svc):
    """The bug: 26h-old print, 24h window, empty series."""
    _print_at(svc, seconds_ago=26 * HOUR, price_micro=220_000)
    history = _history(svc)
    assert len(history) == 1, "the carried-in opening point"
    assert history[0]["p"] == pytest.approx(0.22)


def test_the_carried_point_is_stamped_at_the_window_start_not_its_own_time(svc):
    """Left where it happened it would stretch the axis back over a range the
    caller did not ask for — a 30-day-old print would flatten a one-day chart."""
    _print_at(svc, seconds_ago=26 * HOUR, price_micro=220_000)
    now = int(time.time())
    t = _history(svc)[0]["t"]
    assert t == pytest.approx(now - 24 * HOUR, abs=5)


def test_it_opens_the_window_ahead_of_the_prints_inside_it(svc):
    _print_at(svc, seconds_ago=26 * HOUR, price_micro=220_000)
    _print_at(svc, seconds_ago=2 * HOUR, price_micro=190_000)
    history = _history(svc)
    assert [p["p"] for p in history] == [pytest.approx(0.22), pytest.approx(0.19)]
    assert history[0]["t"] < history[1]["t"]


def test_the_latest_price_before_the_window_wins(svc):
    _print_at(svc, seconds_ago=40 * HOUR, price_micro=500_000)
    _print_at(svc, seconds_ago=26 * HOUR, price_micro=220_000)
    assert _history(svc)[0]["p"] == pytest.approx(0.22)


def test_a_market_that_never_traded_stays_empty(svc):
    """Nothing held, so nothing is carried — the card keeps its placeholder
    rather than inventing a flat day."""
    assert _history(svc) == []


def test_a_window_already_covered_gains_no_extra_point(svc):
    _print_at(svc, seconds_ago=2 * HOUR, price_micro=190_000)
    assert len(_history(svc)) == 1
