"""`OnchainAdmin.peer_transfers` against a stub chain, so what it records is
checked without anvil: which Transfer logs name a wallet, and which never do."""

from types import SimpleNamespace

from web3.constants import ADDRESS_ZERO

from agentpit.onchain.admin import OnchainAdmin

EXCHANGE = "0xE000000000000000000000000000000000000001"
CTF = "0xC000000000000000000000000000000000000002"
ALICE = "0xA11CE00000000000000000000000000000000003"
BOB = "0xB0B0000000000000000000000000000000000004"
CAROL = "0xCA201000000000000000000000000000000000005"


class _Batch:
    def __init__(self, pages):
        self._pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def add(self, request):
        pass

    def execute(self):
        return self._pages


def _admin(*logs: tuple[str, str, int, int]) -> OnchainAdmin:
    """An admin whose chain is at block 100 and holds one page with `logs`,
    each (from, to, amount, block)."""
    transfers = {
        index: SimpleNamespace(args={"from": a, "to": b, "amount": amount}, blockNumber=block)
        for index, (a, b, amount, block) in enumerate(logs)
    }
    web3 = SimpleNamespace(
        batch_requests=lambda: _Batch([list(transfers)]),
        eth=SimpleNamespace(block_number=100, get_logs=lambda params: "placeholder"),
    )
    usd = SimpleNamespace(
        address="0xD0000000000000000000000000000000000000AA",
        events=SimpleNamespace(
            Transfer=lambda: SimpleNamespace(process_log=transfers.__getitem__),
        ),
    )
    usd.events.Transfer.topic = "0xtopic"
    admin = object.__new__(OnchainAdmin)
    admin._client = SimpleNamespace(web3=web3)
    admin._contracts = SimpleNamespace(
        usd=usd, exchange=SimpleNamespace(address=EXCHANGE), ctf=SimpleNamespace(address=CTF)
    )
    return admin


def test_a_transfer_between_two_wallets_names_both_with_its_block():
    found, next_block = _admin((ALICE, BOB, 5_000_000, 42)).peer_transfers(0)

    assert found == {ALICE.lower(): 42, BOB.lower(): 42}
    assert next_block == 101


def test_a_zero_amount_transfer_names_nobody():
    """A stranger can emit one between any two wallets, so it proves nothing."""
    found, _ = _admin((ALICE, BOB, 0, 42)).peer_transfers(0)

    assert found == {}


def test_a_zero_amount_transfer_does_not_date_a_later_real_one():
    found, _ = _admin((ALICE, BOB, 0, 42), (ALICE, CAROL, 1, 77)).peer_transfers(0)

    assert found == {ALICE.lower(): 77, CAROL.lower(): 77}


def test_a_mint_or_a_move_through_the_exchange_or_the_ctf_names_nobody():
    found, _ = _admin(
        (ADDRESS_ZERO, ALICE, 10_000_000, 40),
        (ALICE, EXCHANGE, 3_000_000, 41),
        (CTF, BOB, 3_000_000, 42),
    ).peer_transfers(0)

    assert found == {}
