import pytest

from agentpit.polymarket.format import (
    price_to_decimal_str,
    price_to_float,
    size_to_decimal_str,
    size_to_float,
    decimal_str_to_price_int,
    decimal_str_to_size_micro,
)


def test_price_to_decimal_str_trims_trailing_zeros():
    assert price_to_decimal_str(360000) == "0.36"
    assert price_to_decimal_str(500000) == "0.5"
    assert price_to_decimal_str(1000000) == "1"
    assert price_to_decimal_str(1000) == "0.001"


def test_size_to_decimal_str_whole_and_fractional():
    assert size_to_decimal_str(30000000) == "30"
    assert size_to_decimal_str(30500000) == "30.5"
    assert size_to_decimal_str(0) == "0"


def test_price_to_float():
    assert price_to_float(360000) == pytest.approx(0.36)
    assert price_to_float(1000000) == pytest.approx(1.0)


def test_size_to_float():
    assert size_to_float(30000000) == pytest.approx(30.0)


def test_inverses_round_trip():
    assert decimal_str_to_price_int("0.36") == 360000
    assert decimal_str_to_price_int("0.5") == 500000
    assert decimal_str_to_size_micro("30") == 30000000
    assert decimal_str_to_size_micro("30.5") == 30500000


def test_negative_inputs_raise():
    with pytest.raises(ValueError):
        price_to_decimal_str(-1)
    with pytest.raises(ValueError):
        size_to_float(-1)
    with pytest.raises(ValueError):
        decimal_str_to_price_int("-0.5")


@pytest.mark.parametrize("fn", [decimal_str_to_size_micro, decimal_str_to_price_int])
@pytest.mark.parametrize("value", ["1E+999990", "1e25", "Infinity", "-Infinity", "NaN"])
def test_out_of_range_values_raise_value_error(fn, value):
    with pytest.raises(ValueError):
        fn(value)


def test_largest_allowed_magnitude_still_converts():
    assert decimal_str_to_size_micro("1e24") == 10**30
    assert decimal_str_to_price_int("0.36") == 360000
