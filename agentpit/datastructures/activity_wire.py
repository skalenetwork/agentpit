from pydantic import BaseModel


class ActivityWire(BaseModel):
    """Data-API activity object (§8.10); floats + int-seconds, profile fields
    default to empty so partial data serializes the exact shape."""

    proxyWallet: str = ""
    timestamp: int = 0
    conditionId: str = ""
    type: str = ""           # TRADE | SPLIT | MERGE | REDEEM
    size: float = 0.0
    usdcSize: float = 0.0
    transactionHash: str = ""
    price: float = 0.0
    asset: str = ""
    side: str = ""
    outcomeIndex: int = 0
    title: str = ""
    slug: str = ""
    icon: str = ""
    eventSlug: str = ""
    outcome: str = ""
    name: str = ""
    pseudonym: str = ""
    bio: str = ""
    profileImage: str = ""
    profileImageOptimized: str = ""
    url: str | None = None
