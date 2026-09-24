from pydantic import BaseModel


class AgentSummary(BaseModel):
    handle: str | None
    app: str
    eth_address: str
    created_at: int
