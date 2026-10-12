"""`peer_transfers` on anvil: a wallet-to-wallet apUSD transfer names both
ends, while a mint, approvals and a split through the CTF name nobody."""

from web3 import Web3

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
