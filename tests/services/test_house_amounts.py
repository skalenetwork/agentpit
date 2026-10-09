import random
from decimal import Decimal

from agentpit.onchain.order_signer import OrderData
from agentpit.services.order_service import OrderService, _orders_cross

_M = 1_000_000
_ADDR = "0x00000000000000000000000000000000000000a1"


def _agent(buy: bool, price: int, size: int) -> OrderData:
    maker, taker = OrderService._amounts_from_price_size(
        "BUY" if buy else "SELL", Decimal(price) / _M, size
    )
    return OrderData(
        salt=1,
        maker=_ADDR,
        signer=_ADDR,
        taker=_ADDR,
        tokenId=1,
        makerAmount=maker,
        takerAmount=taker,
        expiration=0,
        nonce=0,
        feeRateBps=0,
        side=0 if buy else 1,
        signatureType=0,
    )


def _level(buy: bool, price: int, rng: random.Random) -> int:
    if rng.random() < 0.3:
        return price
    tick = price // 1000
    return rng.randint(1, tick) * 1000 if buy else rng.randint(tick, 999) * 1000


def test_house_amounts_always_settle():
    rng = random.Random(7)
    for _ in range(20_000):
        buy = rng.random() < 0.5
        price = rng.choice([1000, 999_000, rng.randint(1, 999) * 1000])
        size = rng.choice(
            [rng.randint(1, _M), rng.randint(1, 10**9), rng.randint(1, 1000) * 10_000]
        )
        agent = _agent(buy, price, size)
        side = "BUY" if buy else "SELL"
        remaining = agent.makerAmount
        left = size
        while left:
            q = rng.choice([left, rng.randint(1, left)])
            first = rng.randint(0, q)
            cost = _level(buy, price, rng) * first + _level(buy, price, rng) * (
                q - first
            )
            house = OrderService._house_amounts(agent, q, cost)
            if not 0 < house < q:
                break
            making = q - house if buy else q
            got = q if buy else house
            assert _orders_cross(agent.makerAmount, agent.takerAmount, side, house, q)
            assert making <= remaining
            assert got >= making * agent.takerAmount // agent.makerAmount
            assert not buy or making + house == q
            assert making * _M <= price * q if buy else house * _M >= price * q
            remaining -= making
            left -= q
