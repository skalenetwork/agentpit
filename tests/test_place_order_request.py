"""PlaceOrderRequest: token_id is the canonical (now required) order
identifier; price snaps to the 0.1¢ tick; size is share-denominated with a
one-base-unit floor."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from agentpit.datastructures.place_order_request import PlaceOrderRequest


def _req(price: str) -> PlaceOrderRequest:
    return PlaceOrderRequest(token_id="123", side="BUY", price=price, size=100)


def test_price_snaps_to_tenth_cent_tick():
    assert _req("0.0125").price == Decimal("0.012")  # round-half-to-even
    assert _req("0.1239").price == Decimal("0.124")


def test_on_tick_prices_pass_through_unchanged():
    for p in ("0.001", "0.002", "0.123", "0.5", "0.999"):
        assert _req(p).price == Decimal(p)


def test_rejects_when_snapped_out_of_range():
    for p in ("0.0004", "0.9997"):
        with pytest.raises(ValidationError):
            _req(p)


def test_token_id_required():
    with pytest.raises(ValidationError):
        PlaceOrderRequest(side="BUY", price="0.5", size=100)


def test_size_is_share_denominated_decimal():
    r = PlaceOrderRequest(token_id="123", side="BUY", price="0.5", size="2.5")
    assert r.size == Decimal("2.5")


def test_size_must_be_positive():
    with pytest.raises(ValidationError):
        PlaceOrderRequest(token_id="123", side="BUY", price="0.5", size=0)


def test_size_below_one_base_unit_rejected():
    with pytest.raises(ValidationError):
        PlaceOrderRequest(token_id="123", side="BUY", price="0.5", size="0.0000001")


def test_size_exactly_one_base_unit_ok():
    r = PlaceOrderRequest(token_id="123", side="BUY", price="0.5", size="0.000001")
    assert r.size == Decimal("0.000001")


def test_client_order_id_optional_defaults_none():
    r = PlaceOrderRequest(token_id="123", side="BUY", price="0.5", size=1)
    assert r.client_order_id is None


def test_client_order_id_accepted():
    r = PlaceOrderRequest(
        token_id="123", side="BUY", price="0.5", size=1, client_order_id="abc"
    )
    assert r.client_order_id == "abc"


def _req_size(size: str) -> PlaceOrderRequest:
    return PlaceOrderRequest(token_id="1", side="BUY", price="0.5", size=size)


def test_size_at_cap_is_accepted():
    assert _req_size("1000000000").size == 1_000_000_000


@pytest.mark.parametrize("size", ["1000000001", "1E+999990", "1e30"])
def test_size_above_cap_is_rejected(size):
    with pytest.raises(ValidationError):
        _req_size(size)
