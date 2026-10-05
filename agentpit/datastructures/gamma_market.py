"""Polymarket Gamma-API wire models (the practical subset agentpit serves).

Field names + casing match Gamma exactly. `outcomes`, `outcomePrices`, and
`clobTokenIds` are JSON arrays ENCODED AS STRINGS — that is Gamma's actual
wire format, replicated here so a bot parses agentpit identically to Polymarket.
"""

from pydantic import BaseModel


class GammaMarket(BaseModel):
    id: str
    conditionId: str
    question: str
    slug: str
    description: str
    # Short per-outcome name shown inside an event grouping (e.g. "Spain");
    # null for standalone markets. Gamma's own field name + casing.
    groupItemTitle: str | None = None
    outcomes: str            # JSON-encoded array, e.g. '["Yes","No"]'
    outcomePrices: str       # JSON-encoded array, e.g. '["0.5","0.5"]'
    clobTokenIds: str        # JSON-encoded array of token ids
    active: bool
    closed: bool
    acceptingOrders: bool
    startDate: str | None
    endDate: str | None
    endDateIso: str | None
    icon: str | None
    image: str | None
    volume: str
    liquidity: str
    bestBid: float
    bestAsk: float
    lastTradePrice: float
    spread: float
    winner: str | None = None
    resolvedAt: int | None = None


class GammaEvent(BaseModel):
    id: str
    slug: str
    title: str
    description: str
    icon: str | None
    category: str | None
    startDate: str | None
    endDate: str | None
    # Upstream 24h volume (stringified, Gamma's wire convention). "0" when the
    # event was never synced from upstream. Drives the homepage ordering.
    volume24hr: str = "0"
    # Upstream all-time volume, same wire convention. This is what the cards
    # show; volume24hr stays the ranking key.
    volume: str = "0"
    #: Order-book depth, stringified (Gamma's wire convention). "0" when the
    #: event was never synced from upstream.
    liquidity: str = "0"
    #: How contested the odds are, 0..1, stringified. "0" when never synced.
    competitive: str = "0"
    markets: list[GammaMarket]
