from pydantic import BaseModel, EmailStr, Field

from agentpit.datastructures.attribution import Attribution


class SendCodeRequest(BaseModel):
    email: EmailStr


class CodeSignInRequest(BaseModel):
    email: EmailStr
    # Six digits, as WorkOS issues them. Validated here so an obviously
    # malformed code costs a 422 rather than a round-trip to WorkOS.
    code: str = Field(pattern=r"^\d{6}$")
    # The campaign tags of the link the person arrived by, if any.
    attribution: Attribution | None = None


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class CallbackRequest(BaseModel):
    # The opaque code WorkOS put in the redirect's query string. No shape to
    # validate beyond being present, so an empty one costs a 422 rather than a
    # round-trip.
    code: str = Field(min_length=1)
    attribution: Attribution | None = None
