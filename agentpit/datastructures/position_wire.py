from pydantic import BaseModel


class PositionWire(BaseModel):
    """Data-API position object (§8.8); floats, with defensible defaults so
    partial data still serializes the exact Polymarket shape."""

    proxyWallet: str = ""
    asset: str = ""              # token_id
    conditionId: str = ""
    size: float = 0.0
    avgPrice: float = 0.0
    initialValue: float = 0.0
    currentValue: float = 0.0
    cashPnl: float = 0.0
    percentPnl: float = 0.0
    totalBought: float = 0.0
    realizedPnl: float = 0.0
    percentRealizedPnl: float = 0.0
    curPrice: float = 0.0
    # `currentValue` is a mark (`curPrice` is the book MIDPOINT), which is the
    # right number for a portfolio but not the one to put next to a Sell
    # button: a sale executes against the bids and walks down as it eats
    # depth. These two say what the book would actually pay for the whole
    # position right now, and are 0 when nothing would buy it.
    sellableValue: float = 0.0
    sellableSize: float = 0.0
    settled: bool = False
    redeemable: bool = False
    title: str = ""
    slug: str = ""
    icon: str = ""
    eventSlug: str = ""
    outcome: str = ""
    outcomeIndex: int = 0
    oppositeOutcome: str = ""
    oppositeAsset: str = ""
    endDate: str = ""
    negativeRisk: bool = False
    url: str | None = None
