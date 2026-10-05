from pydantic import BaseModel, Field

from agentpit.datastructures.market_context import MarketContext


class OpenOrder(BaseModel):
    """One element of `GET /data/orders` (CLOB open order, §8.3).

    `owner` is the order owner's non-secret USER_ID (never the api_key).
    `original_size`/`size_matched` are human-decimal share strings.
    """

    id: str
    status: str = "LIVE"
    owner: str
    maker_address: str
    market: str          # condition_id
    asset_id: str        # token_id
    side: str
    original_size: str
    size_matched: str
    price: str
    associate_trades: list[str] = Field(default_factory=list)
    outcome: str
    created_at: int
    expiration: str
    order_type: str


class TitledOpenOrder(OpenOrder, MarketContext):
    title: str
    url: str | None
