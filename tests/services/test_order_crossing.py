"""The matcher's crossing test must match the CTFExchange bit-for-bit
(CalculatorHelper.isCrossing over exact amounts, ONE=1e18, floored), so a DB
match can never trip the on-chain NotCrossing revert that fails the whole order
('No fills'). These pin the replica to the contract's semantics."""

from agentpit.services.order_service import (
    _EXCHANGE_ONE,
    _exchange_price,
    _orders_cross,
)


def test_exchange_price_floors_like_the_contract():
    assert _exchange_price(62, 100, "BUY") == 62 * _EXCHANGE_ONE // 100
    assert _exchange_price(100, 62, "SELL") == 62 * _EXCHANGE_ONE // 100
    assert _exchange_price(5, 0, "BUY") == 0  # zero taker amount -> 0


def test_sell_crosses_equal_and_higher_bids_only():
    assert _orders_cross(100, 40, "SELL", 40, 100)
    assert _orders_cross(100, 40, "SELL", 45, 100)
    assert not _orders_cross(100, 40, "SELL", 35, 100)


def test_mint_boundaries():
    assert _orders_cross(62, 100, "BUY", 40, 100)
    assert _orders_cross(62, 100, "BUY", 38, 100)
    assert not _orders_cross(619, 1000, "BUY", 38, 100)
    assert not _orders_cross(30, 100, "BUY", 40, 100)


def test_zero_taker_amount_is_treated_as_crossing():
    assert _orders_cross(0, 0, "BUY", 38, 100)
