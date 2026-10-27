"""`peer_transfers` on anvil: a wallet-to-wallet apUSD transfer names both
ends, while a mint, approvals, a split through the CTF and a zero-amount
transfer a stranger wrote name nobody."""

from eth_account import Account
from web3 import Web3

from agentpit.onchain.user_wallet import send_user_tx
from tests.onchain import _helpers as h

admin, db = h.admin, h.db  # the shared fixtures


def test_a_gift_names_both_wallets_across_windows_and_batches(admin, db, monkeypatch):
    monkeypatch.setattr("agentpit.onchain.admin._LOG_WINDOW", 3)
    monkeypatch.setattr("agentpit.onchain.admin._LOG_BATCH", 2)
    web3 = admin._client.web3  # noqa: SLF001
    usd = admin._contracts.usd  # noqa: SLF001
    start = web3.eth.block_number + 1
    market = h.synced_market(db, admin)
    giver, taker = h.onboarded_account(db, admin), h.onboarded_account(db, admin)
    h.split(db, admin, giver, market, 1_000_000)
    h.send_as(admin, giver, usd.functions.transfer(Web3.to_checksum_address(taker.eth_address), 5_000_000))
    gift = web3.eth.block_number

    found, next_block = admin.peer_transfers(start)

    assert found == {giver.eth_address.lower(): gift, taker.eth_address.lower(): gift}
    assert next_block == gift + 1


def test_a_zero_transfer_a_stranger_wrote_names_nobody(admin, db):
    """`transferFrom(a, b, 0)` needs no allowance and no balance, so an address
    that holds neither can put a Transfer between any two wallets on chain."""
    web3 = admin._client.web3  # noqa: SLF001
    usd = admin._contracts.usd  # noqa: SLF001
    start = web3.eth.block_number + 1
    a, b = h.onboarded_account(db, admin), h.onboarded_account(db, admin)
    stranger = Account.create()
    admin.fund_gas(stranger.address, 10**16)
    assert usd.functions.balanceOf(stranger.address).call() == 0
    assert usd.functions.allowance(a.eth_address, stranger.address).call() == 0

    receipt = send_user_tx(
        admin._client,  # noqa: SLF001
        stranger,
        usd.functions.transferFrom(
            Web3.to_checksum_address(a.eth_address), Web3.to_checksum_address(b.eth_address), 0
        ),
    )
    written = usd.events.Transfer().process_receipt(receipt)
    assert receipt["status"] == 1
    assert [(w.args["from"], w.args["to"], w.args["amount"]) for w in written] == [
        (Web3.to_checksum_address(a.eth_address), Web3.to_checksum_address(b.eth_address), 0)
    ]

    found, _ = admin.peer_transfers(start)

    assert a.eth_address.lower() not in found
    assert b.eth_address.lower() not in found
