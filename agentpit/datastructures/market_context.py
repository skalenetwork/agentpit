from pydantic import BaseModel


class MarketContext(BaseModel):
    eventTitle: str | None = None
    endDate: int | None = None
    resolvedAt: int | None = None
    winner: str | None = None
    kickoff: int | None = None
    trading: bool | None = None
