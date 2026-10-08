"""`UserGasSponsor` over the real `OnchainAdmin.fund_gas` and the real
`AdminTxSender`, on `tests/fake_skaled.FakeSkaled`.

`test_gas_sponsor.py` fakes the whole chain, so its top-ups never meet the
sender's in-flight slots, its stall healing or its receipt polling. Here the
admin is `_Admin`: an `OnchainAdmin` whose `fund_gas` is the real one, sending
through a real sender, with just enough of the rest (price, estimate, balance,
the user's own send) to run the sponsor. The sender's sleeps are the fake
chain's virtual clock, so the 30 s a top-up may take cost milliseconds.

Numbers: the fake node's price is 200,000 wei, an estimate is 100,000 gas (a
limit of 120,000 after the pad, so a need of 24,000,000,000 wei), and a mined
user call uses 80,000 gas.
"""

from collections import defaultdict
from types import SimpleNamespace

import pytest
from eth_account import Account
from web3 import Web3
from web3.datastructures import AttributeDict
from web3.exceptions import TimeExhausted, Web3RPCError

from agentpit.domain.exceptions import DomainError, GasTopUpTimeoutError
from agentpit.onchain.admin import OnchainAdmin
from agentpit.onchain.tx_sender import TRANSFER_GAS
from agentpit.services.gas_sponsor import UserGasSponsor
from tests.db_helpers import fresh_test_db
from tests.fake_skaled import FakeSkaled, make_sender
from tests.services.test_gas_sponsor import (
    SKALED_BALANCE_LOW,
    _Call,
    _refused,
    _settings,
    _used,
    _user,
)

TIMEOUT = 30  # AGENTPIT_TX_TIMEOUT_S, as `_settings` sets it
PRICE = 200_000  # FakeSkaled.fee
LIMIT = 120_000  # an estimate of 100,000 plus 20%
NEED = LIMIT * PRICE
GAS_USED = 80_000
MINED = TRANSFER_GAS + GAS_USED  # one top-up and one mined call
RESERVED = LIMIT + TRANSFER_GAS  # what a split holds while it sends


class _Admin(OnchainAdmin):
    """The sponsor's view of the chain, on a real sender.

    `fund_gas` is `OnchainAdmin.fund_gas` itself, reaching the sender through
    a client stub, so what it passes the sender is under test. The wallet's
    native balance is what the chain mined to it, less what its own sends cost
    (gas used at the max fee, as skaled bills). A user send is refused at
    import with skaled's wording when the wallet could not pay its limit at
    its max fee.
    """

    def __init__(self, chain: FakeSkaled, sender):
        super().__init__(SimpleNamespace(admin_sender=sender), None)  # type: ignore[arg-type]
        self.chain = chain
        self.spent: dict[str, int] = defaultdict(int)
        self.user_sends: list[str] = []

    def gas_price(self) -> int:
        return self.chain.fee[0]

    def estimate_user_gas(self, fn, address: str) -> int:
        return 100_000

    def native_balance(self, address: str) -> int:
        received = sum(
            item["tx"]["value"]
            for item in self.chain.accepted
            if item["hash"] in self.chain.mined
            and Web3.to_checksum_address(item["tx"]["to"])
            == Web3.to_checksum_address(address)
        )
        return received - self.spent[address.lower()]

    def send_as_user(
        self, user_account, fn, *, gas, max_fee, timeout=30, on_signed=None
    ):
        address = user_account.address
        if self.native_balance(address) < gas * max_fee:
            raise _refused(SKALED_BALANCE_LOW)
        self.user_sends.append(address.lower())
        self.spent[address.lower()] += GAS_USED * max_fee
        return AttributeDict({"status": 1, "gasUsed": GAS_USED})

    def topped_up(self, address: str) -> list[int]:
        """Every transfer to `address` the node accepted, mined or not."""
        return [
            item["tx"]["value"]
            for item in self.chain.accepted
            if Web3.to_checksum_address(item["tx"]["to"])
            == Web3.to_checksum_address(address)
        ]


def _setup(*, mine_on_sleep: bool, **sender_kw):
    chain = FakeSkaled()
    sender, _account, clock = make_sender(
        chain, mine_on_sleep=mine_on_sleep, max_in_flight=4, **sender_kw
    )
    admin = _Admin(chain, sender)
    db = fresh_test_db()
    sponsor = UserGasSponsor(db, admin, _settings())
    return chain, sender, clock, admin, db, sponsor


def _send(sponsor, user, kind="claim"):
    with sponsor.locked(user):
        return sponsor.send(user, [_Call()], kind)  # type: ignore[list-item]


def _fill_the_pipeline(sender) -> None:
    """Four transfers that nothing mines: every slot is taken."""
    other = Account.create().address
    for _ in range(4):
        sender.submit_value(other, 1)


# --- the pipeline is full ---------------------------------------------------


@pytest.mark.parametrize(("kind", "standing"), [("claim", 0), ("split", RESERVED)])
def test_a_full_pipeline_fails_the_top_up_within_the_sponsors_timeout(kind, standing):
    """Waiting for a slot used to come first and take the sender's own 120 s,
    then the receipt took up to 30 s more, all with the user's lock held. The
    sponsor's timeout now covers both."""
    chain, sender, clock, admin, db, sponsor = _setup(
        mine_on_sleep=False, slot_timeout=120
    )
    user = _user(db)
    _fill_the_pipeline(sender)
    started = clock()

    with pytest.raises(GasTopUpTimeoutError):
        _send(sponsor, user, kind)

    assert clock() - started <= TIMEOUT + 1
    # Nothing was broadcast for the user: no transfer, no transaction of theirs.
    assert admin.topped_up(user.eth_address) == []
    assert admin.user_sends == []
    assert len(chain.accepted) == 4
    with sponsor.locked(user):  # the lock is free
        pass
    # Nothing went out, so a split's reservation stands only as an over-count.
    assert _used(db, user) == standing


def test_the_sponsor_passes_its_timeout_as_the_slot_timeout():
    """`OnchainAdmin.fund_gas` is the one place that sets the bound."""
    calls: list[dict] = []

    class _Sender:
        def send_value(self, to, value_wei, **kwargs):
            calls.append({"to": to, "value": value_wei, **kwargs})
            return AttributeDict({"status": 1})

    OnchainAdmin(SimpleNamespace(admin_sender=_Sender()), None).fund_gas(  # type: ignore[arg-type]
        "0x" + "11" * 20, 5, timeout=7
    )
    assert calls == [
        {"to": "0x" + "11" * 20, "value": 5, "timeout": 7, "slot_timeout": 7}
    ]


# --- the node lost the top-up -----------------------------------------------


@pytest.mark.parametrize("kind", ["claim", "split"])
def test_a_top_up_the_node_lost_is_a_retryable_503_and_the_next_send_funds_once(kind):
    """The node said OK to the top-up and never queued it. The sender fills the
    nonce with a gap filler and reports the top-up `TxDropped`. The sponsor
    answers "busy, try again" (503) and, as nothing mined, hands a split's
    reservation back. The retry tops up once more, and that one lands."""
    chain, sender, clock, admin, db, sponsor = _setup(
        mine_on_sleep=True, stall_after=5
    )
    user = _user(db)
    chain.lose.add(0)  # the first transaction the admin sends

    with pytest.raises(GasTopUpTimeoutError) as caught:
        _send(sponsor, user, kind)

    assert isinstance(caught.value, DomainError)
    assert admin.user_sends == []
    assert _used(db, user) == 0  # nothing mined: a split's reservation is back
    with sponsor.locked(user):  # and the lock is free
        pass

    _send(sponsor, user, kind)

    # Two transfers were accepted: the lost one and the one sent again.
    assert admin.topped_up(user.eth_address) == [NEED, NEED]
    assert admin.user_sends == [user.eth_address.lower()]
    assert admin.native_balance(user.eth_address) == NEED - GAS_USED * PRICE


# --- a top-up that mines after the sponsor gave up --------------------------


@pytest.mark.parametrize(
    ("kind", "standing"), [("claim", 0), ("split", RESERVED)]
)
def test_a_top_up_that_mines_late_is_used_by_the_next_send_not_paid_again(
    kind, standing
):
    """The receipt did not come in time: 503, nothing of the user's sent, and
    the sender still holds the slot. Once the block comes the next send reads
    the funded wallet, tops up nothing, and sends once."""
    chain, sender, clock, admin, db, sponsor = _setup(mine_on_sleep=False)
    user = _user(db)

    with pytest.raises(GasTopUpTimeoutError) as caught:
        _send(sponsor, user, kind)

    assert isinstance(caught.value.__cause__, TimeExhausted)
    assert sender._in_flight_count() == 1  # noqa: SLF001 - the top-up's slot
    assert admin.user_sends == []
    assert _used(db, user) == standing

    chain.mine()  # the top-up lands after all
    sender.poll()
    assert sender._in_flight_count() == 0  # noqa: SLF001 - its slot is free again

    _send(sponsor, user, kind)

    assert admin.topped_up(user.eth_address) == [NEED]  # not paid twice
    assert admin.user_sends == [user.eth_address.lower()]
    # A split's reservation stood across the timeout; the claim held none. The
    # late transfer itself is booked by neither: the sponsor never saw it land.
    assert _used(db, user) == standing + GAS_USED


def test_the_stand_in_refuses_a_user_send_from_a_dry_wallet():
    """The oracle the tests above lean on: the stand-in is no yes-man."""
    chain, sender, clock, admin, db, sponsor = _setup(mine_on_sleep=True)
    with pytest.raises(Web3RPCError):
        admin.send_as_user(
            Account.create(), None, gas=LIMIT, max_fee=PRICE  # type: ignore[arg-type]
        )
