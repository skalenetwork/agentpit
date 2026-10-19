"""Convert agentpit's internal scaled-integer money values to/from the
Polymarket wire representations.

agentpit stores prices and sizes as integers scaled by 10**6
(10**6 == $1.00, and 1 outcome token == 10**6 base units). Polymarket's
CLOB family uses decimal STRINGS ("0.36", "30"); its Data-API and
prices-history families use JSON floats (0.36, 30.0).
"""

from decimal import ROUND_HALF_UP, Decimal

_SCALE = Decimal(10**6)

# int() of a Decimal with a huge exponent builds a huge integer; refuse
# anything above ~10**24 before converting.
_MAX_ADJUSTED = 24


def _trim(d: Decimal) -> str:
    """Fixed-point string with no exponent and no trailing zeros.

    Decimal.normalize() would render 30 as '3E+1', so format with 'f'
    and strip trailing zeros manually.
    """
    s = format(d, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _require_non_negative(value: int, name: str) -> None:
    if value < 0:
        raise ValueError(f"{name} must be non-negative, got {value}")


def price_to_decimal_str(price_int: int) -> str:
    """360000 -> '0.36' (USDC-per-share decimal string, 0..1)."""
    _require_non_negative(price_int, "price_int")
    return _trim(Decimal(price_int) / _SCALE)


def price_to_float(price_int: int) -> float:
    """360000 -> 0.36 (for the Data-API / prices-history families)."""
    _require_non_negative(price_int, "price_int")
    return float(Decimal(price_int) / _SCALE)


def size_to_decimal_str(micro: int) -> str:
    """30000000 -> '30' (whole-share decimal string)."""
    _require_non_negative(micro, "micro")
    return _trim(Decimal(micro) / _SCALE)


def size_to_float(micro: int) -> float:
    """30000000 -> 30.0 (for the Data-API family)."""
    _require_non_negative(micro, "micro")
    return float(Decimal(micro) / _SCALE)


def _bounded(value: str, name: str) -> Decimal:
    d = Decimal(value)
    if not d.is_finite() or d.adjusted() > _MAX_ADJUSTED:
        raise ValueError(f"{name} out of range")
    return d


def decimal_str_to_price_int(value: str) -> int:
    """'0.36' -> 360000 (parse an inbound decimal price to scaled int)."""
    scaled = int((_bounded(value, "price") * _SCALE).to_integral_value(rounding=ROUND_HALF_UP))
    _require_non_negative(scaled, "price")
    return scaled


def decimal_str_to_size_micro(value: str) -> int:
    """'30' -> 30000000 (parse an inbound decimal share size to base units)."""
    scaled = int((_bounded(value, "size") * _SCALE).to_integral_value(rounding=ROUND_HALF_UP))
    _require_non_negative(scaled, "size")
    return scaled
