"""End-to-end: a market with a live Polymarket book surfaces book-derived prices
(outcomePrices/bestBid/bestAsk/spread) through the Gamma event serialization,
instead of the neutral 0.5 placeholder."""

from agentpit.api.deps import get_db_session
from agentpit.api.main import app
from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.market_state import MarketState
from agentpit.db.table_write import TableWrite
from agentpit.services.event_service import EventService


def _hex32(seed: str) -> str:
    return "0x" + seed.encode().hex().ljust(64, "0")[:64]


def test_list_events_gamma_reports_book_derived_prices(house_book):
    session = app.dependency_overrides[get_db_session]()
    req = CreateMarketRequest(
        question="Will it rain?",
        description="d",
        erc1155_tokens=[("p1", "Yes"), ("p2", "No")],
        slug="will-it-rain",
        condition_id=ConditionId(_hex32("c1")),
        state=MarketState.ACTIVE,
    )
    with session.write() as conn:
        market = TableWrite.create_market(conn, req, is_polygon_market=False)
        event = TableWrite.upsert_event(
            conn, slug="will-it-rain", title="Will it rain?"
        )
        TableWrite.attach_market_to_event(
            conn, market_id=market.market_id, event_id=event.event_id
        )
    house_book("p1", bids=(("0.14", "5"),), asks=(("0.15", "5"),))

    events = EventService(session).list_events_gamma(limit=10, offset=0)
    m = events[0].markets[0]
    assert m.outcomePrices == '["0.145","0.855"]'
    assert m.bestBid == 0.14
    assert m.bestAsk == 0.15
    assert m.spread == 0.01


def test_list_events_gamma_keeps_placeholder_without_book():
    session = app.dependency_overrides[get_db_session]()
    req = CreateMarketRequest(
        question="Will it snow?",
        description="d",
        erc1155_tokens=[("q1", "Yes"), ("q2", "No")],
        slug="will-it-snow",
        condition_id=ConditionId(_hex32("c2")),
        state=MarketState.ACTIVE,
    )
    with session.write() as conn:
        market = TableWrite.create_market(conn, req, is_polygon_market=False)
        event = TableWrite.upsert_event(
            conn, slug="will-it-snow", title="Will it snow?"
        )
        TableWrite.attach_market_to_event(
            conn, market_id=market.market_id, event_id=event.event_id
        )

    events = EventService(session).list_events_gamma(limit=10, offset=0)
    m = events[0].markets[0]
    assert m.outcomePrices == '["0.5","0.5"]'  # no book -> neutral fallback
    assert m.bestBid == 0.0 and m.bestAsk == 0.0
