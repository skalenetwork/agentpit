from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# Minimum price increment: 0.1¢ = $0.001. Prices snap to this grid so the book
# can't accumulate sub-tick precision — the minimum meaningful step is 0.1¢.
_PRICE_TICK = Decimal("0.001")


class PlaceOrderRequest(BaseModel):
    """Logical inputs for `POST /order` (§8.1).

    `token_id` is the canonical outcome identifier. `size` is whole shares
    (converted to 10⁶ base units in the service).
    """

    token_id: str = Field(min_length=1)
    side: Literal["BUY", "SELL"]
    price: Decimal = Field(gt=0, lt=1)  # probability, 0 < p < 1
    size: Decimal = Field(gt=0)  # whole shares (× 10⁶ base units internally)
    order_type: Literal["GTC", "FOK", "FAK", "GTD"] = "GTC"
    expiration: int = Field(default=0, ge=0)
    client_order_id: str | None = None  # optional per-user idempotency key

    @field_validator("price")
    @classmethod
    def _snap_to_tick(cls, v: Decimal) -> Decimal:
        """Round the price onto the 0.1¢ tick. Reject only if snapping
        leaves the open interval (0, 1)."""
        snapped = v.quantize(_PRICE_TICK)
        if snapped <= 0 or snapped >= 1:
            raise ValueError("price must be within (0, 1) on the 0.1¢ tick")
        return snapped

    @field_validator("size")
    @classmethod
    def _min_one_base_unit(cls, v: Decimal) -> Decimal:
        """Reject sizes below one base unit (10⁻⁶ shares) — they would scale
        to zero in the 10⁶-unit internal representation."""
        if v < Decimal("0.000001"):
            raise ValueError("size must be at least 0.000001 shares (one base unit)")
        return v

    @model_validator(mode="after")
    def _expiration_agrees_with_type(self) -> "PlaceOrderRequest":
        """`expiration` is only meaningful for GTD, and GTD requires one.

        Both directions are rejections rather than corrections. A GTD with no
        expiration is a caller who forgot a field, and reading it as "never"
        gives them the opposite of what they asked for; an expiration on a
        GTC is a caller who thinks they set a lifetime, and dropping it
        quietly means they find out when the order outlives them.
        """
        if self.order_type == "GTD" and self.expiration == 0:
            raise ValueError("a GTD order requires a non-zero expiration")
        if self.order_type != "GTD" and self.expiration != 0:
            raise ValueError("expiration is only valid on a GTD order")
        return self
