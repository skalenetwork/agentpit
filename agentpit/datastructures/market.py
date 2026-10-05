from typing import Optional, Any
from pydantic import BaseModel, field_validator

from agentpit.common import check_state
from agentpit.datastructures.condition_id import ConditionId
from agentpit.db.table_create import MarketState
from agentpit.polymarket.conditional_token_framework import ConditionalTokenFramework
from agentpit.utils.parse import is_hex256


class Market(BaseModel):
    question: str
    slug: str
    market_id: int
    polymarket_id: Optional[int] = None
    polymarket_condition_id: Optional[str] = None
    polymarket_yes_token_id: Optional[str] = None
    polymarket_no_token_id: Optional[str] = None
    condition_id: ConditionId = None
    description: str
    erc1155_tokens: list[tuple[str, str]]
    start_date: int
    end_date: Optional[int | None]
    resolved_outcome: Optional[int] = None
    market_state: MarketState
    event_id: Optional[int] = None
    outcome_label: Optional[str] = None
    icon_url: Optional[str] = None
    fully_redeemed: bool = False
    price_change_24h: float | None = None
    resolved_at: int | None = None

    @property
    def url(self) -> str | None:
        if self.polymarket_id is None:
            return None
        return f"https://polymarket.com/market/{self.slug}"

    def model_post_init(self, __context):
        check_state(
            self.market_state != MarketState.RESOLVED
            or self.resolved_outcome is not None,
            "Resolved market must have an outcome",
        )
        check_state(len(self.question) > 0, "Question must not be empty")
        check_state(
            len(self.erc1155_tokens) > 0, "Must have at least one ERC1155 token"
        )
        check_state(
            self.end_date is None or self.end_date > self.start_date,
            "End date must be after start date",
        )
        check_state(self.condition_id is not None)
