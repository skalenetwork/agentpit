"""Serialize agentpit Market/Event domain objects into Gamma wire models.

Price fields (outcomePrices/bestBid/bestAsk/lastTradePrice/spread) are filled
from a MarketPrices when the caller supplies one (derived from the local book +
trade tape); without it they fall back to neutral placeholders (0.5 / 0.0).
volume/liquidity remain placeholders pending their own wiring.
"""

import json
from datetime import datetime, timezone

from agentpit.datastructures.event import Event
from agentpit.datastructures.gamma_market import GammaEvent, GammaMarket
from agentpit.datastructures.market import Market
from agentpit.datastructures.market_state import MarketState
from agentpit.polymarket.format import price_to_decimal_str, price_to_float
from agentpit.polymarket.pricing import MarketPrices

_CLOSED_STATES = (MarketState.CLOSED, MarketState.RESOLVED, MarketState.CANCELLED)


def _iso(epoch: int | None) -> str | None:
    if epoch is None:
        return None
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _json_arr(items: list[str]) -> str:
    """Compact JSON array string (no spaces), matching Gamma's encoding."""
    return json.dumps(items, separators=(",", ":"))


def _price_fields(prices: "MarketPrices | None", labels: list[str]) -> dict:
    """The price-derived GammaMarket fields. With no prices — or an outcome
    count that doesn't match — fall back to neutral placeholders (0.5 / 0.0)."""
    if prices is None or len(prices.outcome_prices) != len(labels):
        return {
            "outcomePrices": _json_arr(["0.5" for _ in labels]),
            "bestBid": 0.0,
            "bestAsk": 0.0,
            "lastTradePrice": 0.0,
            "spread": 0.0,
        }
    best_bid = price_to_float(prices.best_bid) if prices.best_bid is not None else 0.0
    best_ask = price_to_float(prices.best_ask) if prices.best_ask is not None else 0.0
    spread = (
        round(best_ask - best_bid, 6)
        if prices.best_bid is not None and prices.best_ask is not None
        else 0.0
    )
    last_trade = (
        price_to_float(prices.last_trade) if prices.last_trade is not None else 0.0
    )
    return {
        "outcomePrices": _json_arr(
            [price_to_decimal_str(p) for p in prices.outcome_prices]
        ),
        "bestBid": best_bid,
        "bestAsk": best_ask,
        "lastTradePrice": last_trade,
        "spread": spread,
    }


def to_gamma_market(
    market: Market, prices: "MarketPrices | None" = None
) -> GammaMarket:
    labels = [label for _token_id, label in market.erc1155_tokens]
    token_ids = [token_id for token_id, _label in market.erc1155_tokens]
    active = market.market_state == MarketState.ACTIVE
    closed = market.market_state in _CLOSED_STATES
    end_iso = _iso(market.end_date)
    price_fields = _price_fields(prices, labels)
    if market.payouts is not None:
        price_fields["outcomePrices"] = _json_arr([f"{n / sum(market.payouts):g}" for n in market.payouts])
    return GammaMarket(
        id=str(market.market_id),
        conditionId=market.condition_id.value,
        question=market.question,
        slug=market.slug,
        description=market.description,
        groupItemTitle=market.outcome_label,
        outcomes=_json_arr(labels),
        clobTokenIds=_json_arr(token_ids),
        active=active,
        closed=closed,
        acceptingOrders=active,
        startDate=_iso(market.start_date),
        endDate=end_iso,
        endDateIso=end_iso,
        icon=market.icon_url,
        image=market.icon_url,
        volume="0",
        liquidity="0",
        winner=market.winner,
        resolvedAt=market.resolved_at,
        **price_fields,
    )


def to_gamma_event(
    event: Event,
    markets: list[Market],
    prices_by_market: "dict[int, MarketPrices] | None" = None,
) -> GammaEvent:
    pbm = prices_by_market or {}
    return GammaEvent(
        id=str(event.event_id),
        slug=event.slug,
        title=event.title,
        description=event.description,
        icon=event.icon_url,
        category=event.category,
        startDate=_iso(event.start_date),
        endDate=_iso(event.end_date),
        volume24hr=str(event.volume_24hr if event.volume_24hr is not None else 0),
        volume=str(event.volume if event.volume is not None else 0),
        liquidity=str(event.liquidity if event.liquidity is not None else 0),
        competitive=str(event.competitive if event.competitive is not None else 0),
        markets=[to_gamma_market(m, pbm.get(m.market_id)) for m in markets],
    )
