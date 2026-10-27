from enum import Enum


class MarketState(str, Enum):
    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"
    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"


CLOSED_STATES = (MarketState.CLOSED, MarketState.RESOLVED, MarketState.CANCELLED)
