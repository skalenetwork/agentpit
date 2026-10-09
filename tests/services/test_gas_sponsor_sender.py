"""`UserGasSponsor` over the real `OnchainAdmin.fund_gas` and the real
`AdminTxSender`, on `tests/fake_skaled.FakeSkaled`.

`test_gas_sponsor.py` fakes the whole chain, so its top-ups never meet the
sender's in-flight slots, stall healing or receipt polling. `_Admin` is an
`OnchainAdmin` whose `fund_gas` is the real one, with just enough of the rest to
run the sponsor; the sender's sleeps are the fake chain's virtual clock, so 30 s
cost milliseconds.
"""

from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest
from eth_account import Account
from web3 import Web3
from web3.datastructures import AttributeDict

from agentpit.domain.exceptions import DomainError, GasTopUpTimeoutError
from agentpit.onchain.admin import OnchainAdmin
from agentpit.onchain.tx_sender import TRANSFER_GAS, AdminTxSender
from tests.db_helpers import fresh_test_db
from tests.fake_skaled import FakeSkaled, make_sender
from tests.services.test_gas_sponsor import (
    SKALED_BALANCE_LOW,
    _refused,
    _send,
    _sponsor,
    _used,
    _user,
)

PRICE = 200_000  # FakeSkaled.fee
LIMIT = 120_000  # an estimate of 100,000 plus 20%
NEED = LIMIT * PRICE
GAS_USED = 80_000
RESERVED = LIMIT + TRANSFER_GAS  # what a split holds while it sends


class _Admin(OnchainAdmin):
    """The sponsor's view of the chain, on a real sender. `fund_gas` is
    `OnchainAdmin.fund_gas` itself, reaching the sender through a client stub.
    The wallet's native balance is what the chain mined to it, less what its own
    sends cost (gas used at the max fee, as skaled bills); a user send is refused
    at import, in skaled's wording, when the wallet could not pay its limit."""

    def __init__(self, chain: FakeSkaled, sender):
        super().__init__(SimpleNamespace(admin_sender=sender), None)  # type: ignore[arg-type]
        self.chain = chain
        self.spent: dict[str, int] = defaultdict(int)
        self.user_sends: list[str] = []

    def gas_price(self) -> int:
        return self.chain.fee[0]

    def estimate_user_gas(self, fn, address: str) -> int:
        return 100_000

    def topped_up(self, address: str, *, mined: bool = False) -> list[int]:
        """Transfers to `address` the node accepted (mined ones only with `mined`)."""
        to = Web3.to_checksum_address(address)
        return [
            item["tx"]["value"]
            for item in self.chain.accepted
            if Web3.to_checksum_address(item["tx"]["to"]) == to
            and (not mined or item["hash"] in self.chain.mined)
        ]

    def native_balance(self, address: str) -> int:
        return sum(self.topped_up(address, mined=True)) - self.spent[address.lower()]

    def send_as_user(self, user_account, fn, *, gas, max_fee, **_):
        address = user_account.address
        if self.native_balance(address) < gas * max_fee:
            raise _refused(SKALED_BALANCE_LOW)
        self.user_sends.append(address.lower())
        self.spent[address.lower()] += GAS_USED * max_fee
        return AttributeDict({"status": 1, "gasUsed": GAS_USED})


def _setup(**sender_kw):
    chain = FakeSkaled()
    sender, _, clock = make_sender(chain, max_in_flight=4, **sender_kw)
    db = fresh_test_db()
    return chain, sender, clock, _Admin(chain, sender), db, _user(db)


@pytest.mark.parametrize(("kind", "standing"), [("claim", 0), ("split", RESERVED)])
def test_a_full_pipeline_fails_the_top_up_within_the_sponsors_timeout(kind, standing):
    # Waiting for a slot used to take the sender's own 120 s first, then up to
    # 30 s more for the receipt, all with the user's lock held.
    chain, sender, clock, admin, db, user = _setup(mine_on_sleep=False, slot_timeout=120)
    other = Account.create().address
    for _ in range(4):  # four transfers nothing mines: every slot is taken
        sender.submit_value(other, 1)
    started = clock()

    _send(db, user, admin, kind, raises=GasTopUpTimeoutError)

    assert clock() - started <= 30 + 1  # AGENTPIT_TX_TIMEOUT_S, plus a tick
    # Nothing was broadcast for the user: no transfer, no transaction of theirs.
    assert admin.topped_up(user.eth_address) == []
    assert admin.user_sends == []
    assert len(chain.accepted) == 4
    with _sponsor(db, admin).locked(user):  # the lock is free
        pass
    # Nothing went out, so a split's reservation stands only as an over-count.
    assert _used(db, user) == standing


def test_the_sponsor_passes_its_timeout_as_the_slot_timeout():
    # `OnchainAdmin.fund_gas` is the one place that sets the bound.
    sender = create_autospec(AdminTxSender, instance=True)
    admin = OnchainAdmin(SimpleNamespace(admin_sender=sender), None)  # type: ignore[arg-type]
    address = "0x" + "11" * 20
    admin.fund_gas(address, 5, timeout=7)
    sender.send_value.assert_called_once_with(address, 5, timeout=7, slot_timeout=7)


@pytest.mark.parametrize("kind", ["claim", "split"])
def test_a_top_up_the_node_lost_is_a_retryable_503_and_the_next_send_funds_once(kind):
    # The node said OK to the top-up and never queued it. The sender fills the
    # nonce with a gap filler and reports `TxDropped`; the sponsor answers "busy,
    # try again" (503) and, as nothing mined, hands a split's reservation back.
    chain, _, _, admin, db, user = _setup(stall_after=5)
    chain.lose.add(0)  # the first transaction the admin sends

    error = _send(db, user, admin, kind, raises=GasTopUpTimeoutError)

    assert isinstance(error, DomainError)
    assert admin.user_sends == []
    assert _used(db, user) == 0  # nothing mined: a split's reservation is back
    with _sponsor(db, admin).locked(user):  # and the lock is free
        pass

    _send(db, user, admin, kind)

    # Two transfers were accepted: the lost one and the one sent again.
    assert admin.topped_up(user.eth_address) == [NEED, NEED]
    assert admin.user_sends == [user.eth_address.lower()]
    assert admin.native_balance(user.eth_address) == NEED - GAS_USED * PRICE
