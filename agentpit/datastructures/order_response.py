from pydantic import BaseModel, Field, field_validator


class OrderResponse(BaseModel):
    """Polymarket CLOB `postOrder` response shape (§8.1).

    `status` is the documented HTTP enum (lowercase): `live | matched |
    delayed`. agentpit emits `live`, `matched` and `cancelled`. A settlement
    failure is reported as `success=False` + `errorMsg` (not a status).
    """

    success: bool
    errorMsg: str = ""
    orderID: str
    status: str
    transactionsHashes: list[str] = Field(default_factory=list)
    takingAmount: str = ""
    makingAmount: str = ""
    tradeIDs: list[str] = Field(default_factory=list)

    @field_validator("orderID", "status")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("must not be empty")
        return v
