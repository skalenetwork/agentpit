from pydantic import BaseModel, Field

# A campaign tag is a short slug or an agent address (a referral link carries
# the referring agent's address). Anything else is dropped at the door, so a
# junk or hostile value never reaches the database or an analytics payload.
TAG = r"^(?:[A-Za-z0-9_.\-]{1,40}|0x[0-9a-fA-F]{40})$"


class Attribution(BaseModel):
    """Where a person first came from: the `ref` and UTM tags of the link they
    arrived by, carried from agentpit.dev through sign-in. Every field is
    optional, and an owner keeps the first value seen for each."""

    ref: str | None = Field(default=None, pattern=TAG)
    utm_source: str | None = Field(default=None, pattern=TAG)
    utm_medium: str | None = Field(default=None, pattern=TAG)
    utm_campaign: str | None = Field(default=None, pattern=TAG)
