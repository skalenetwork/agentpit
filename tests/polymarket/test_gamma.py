import json

from agentpit.datastructures.event import Event
from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.market import Market
from agentpit.datastructures.market_state import MarketState
from agentpit.polymarket.gamma import to_gamma_market, to_gamma_event


def _market(state=MarketState.ACTIVE, outcome_label=None) -> Market:
    return Market(
        question="Will it rain?",
        slug="will-it-rain",
        market_id=7,
        condition_id=ConditionId("0x" + "ab" * 32),
        question_id="0x" + "cd" * 32,
        description="desc",
        erc1155_tokens=[("111", "Yes"), ("222", "No")],
        start_date=1_700_000_000,
        end_date=1_800_000_000,
        market_state=state,
        payouts=(1, 0) if state == MarketState.RESOLVED else None,
        outcome_label=outcome_label,
    )


def test_to_gamma_market_emits_group_item_title_from_outcome_label():
    # `groupItemTitle` is the short per-outcome name shown inside an event
    # (e.g. "Spain") — Gamma's own field, mirrored from the stored outcome_label.
    g = to_gamma_market(_market(outcome_label="Spain"))
    assert g.groupItemTitle == "Spain"
    # Absent for standalone markets (no event short name).
    assert to_gamma_market(_market()).groupItemTitle is None


def test_to_gamma_market_shape_and_encoding():
    g = to_gamma_market(_market())
    assert g.id == "7"
    assert g.conditionId == "0x" + "ab" * 32
    assert g.question == "Will it rain?"
    # JSON-encoded string arrays (Gamma's quirk), compact (no spaces)
    assert g.outcomes == '["Yes","No"]'
    assert g.clobTokenIds == '["111","222"]'
    assert json.loads(g.outcomes) == ["Yes", "No"]
    assert g.active is True
    assert g.closed is False
    assert g.acceptingOrders is True
    assert g.endDateIso == g.endDate  # endDateIso mirrors endDate
    assert g.endDateIso.endswith("Z")  # ISO8601 UTC
    assert g.volume == "0"
    assert g.bestBid == 0.0


def test_to_gamma_market_closed_states():
    for state in (MarketState.CLOSED, MarketState.RESOLVED, MarketState.CANCELLED):
        g = to_gamma_market(_market(state))
        assert g.active is False
        assert g.closed is True
        assert g.acceptingOrders is False


def test_a_resolved_market_prices_its_payouts():
    won = to_gamma_market(_market(MarketState.RESOLVED))
    split = to_gamma_market(_market(MarketState.RESOLVED).model_copy(update={"payouts": (1, 1)}))
    assert (won.winner, won.outcomePrices) == ("Yes", '["1","0"]')
    assert (split.winner, split.outcomePrices) == (None, '["0.5","0.5"]')


def test_to_gamma_event_nests_markets():
    g = to_gamma_event(
        Event(event_id=3, slug="weather", title="Weather", description="d"),
        [_market()],
    )
    assert g.id == "3"
    assert g.slug == "weather"
    assert len(g.markets) == 1
    assert g.markets[0].conditionId == "0x" + "ab" * 32


def test_to_gamma_market_uses_prices_when_given():
    from agentpit.polymarket.pricing import PRICE_ONE, MarketPrices

    prices = MarketPrices(
        best_bid=140_000,
        best_ask=150_000,
        last_trade=145_000,
        outcome_prices=[145_000, PRICE_ONE - 145_000],
    )
    g = to_gamma_market(_market(), prices)
    assert g.outcomePrices == '["0.145","0.855"]'
    assert g.bestBid == 0.14
    assert g.bestAsk == 0.15
    assert g.lastTradePrice == 0.145
    assert g.spread == 0.01


def test_to_gamma_market_placeholder_without_prices():
    # No MarketPrices -> the neutral 0.5 / 0.0 fallback (unchanged behaviour).
    g = to_gamma_market(_market())
    assert g.outcomePrices == '["0.5","0.5"]'
    assert g.bestBid == 0.0 and g.bestAsk == 0.0
    assert g.lastTradePrice == 0.0 and g.spread == 0.0


def test_to_gamma_event_threads_prices_by_market_id():
    from agentpit.polymarket.pricing import PRICE_ONE, MarketPrices

    m = _market()
    prices = {
        m.market_id: MarketPrices(
            best_bid=600_000,
            best_ask=620_000,
            last_trade=None,
            outcome_prices=[610_000, PRICE_ONE - 610_000],
        )
    }
    g = to_gamma_event(
        Event(event_id=3, slug="weather", title="Weather", description="d"),
        [m],
        prices,
    )
    assert g.markets[0].outcomePrices == '["0.61","0.39"]'
    assert g.markets[0].bestBid == 0.6
