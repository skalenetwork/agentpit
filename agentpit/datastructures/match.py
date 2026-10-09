from dataclasses import dataclass
from typing import Literal

from agentpit.onchain.order_signer import OrderData


@dataclass(frozen=True, slots=True)
class Take:
    ask: bool
    price: int
    shown: int
    size: int


@dataclass(frozen=True, slots=True)
class Match:
    takes: tuple[Take, ...]
    size: int
    house_amount: int
    agent_amount: int
    kind: Literal["NORMAL", "MINT"]
    house_order: OrderData
    house_signature: bytes
    trade_id: str
