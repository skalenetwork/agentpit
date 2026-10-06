"""The admin key lands several transactions in one block, and the blocking
`send_admin_tx` path still behaves as before."""

import secrets

from eth_account import Account

from agentpit.config import Settings
from agentpit.onchain.admin import OnchainAdmin
from agentpit.onchain.contracts import Contracts
from agentpit.onchain.deployment import Deployment
from agentpit.onchain.web3_client import Web3Client


def _admin() -> OnchainAdmin:
    settings = Settings()
    d = Deployment.load(settings.deployment_path)
    client = Web3Client(settings, d)
    return OnchainAdmin(client, Contracts(client.web3, d))


def test_admin_txs_share_one_block():
    admin = _admin()
    client = admin._client  # noqa: SLF001
    w3 = client.web3
    sender = client.admin_sender
    w3.provider.make_request("evm_setAutomine", [False])
    try:
        pendings = [sender.submit_value(client.admin.address, 0) for _ in range(5)]
        w3.provider.make_request("evm_mine", [])
        receipts = sender.wait_all(pendings, timeout=30)
    finally:
        w3.provider.make_request("evm_setAutomine", [True])
    assert all(r["status"] == 1 for r in receipts)
    assert len({r["blockNumber"] for r in receipts}) == 1
    first = pendings[0].nonce
    assert [p.nonce for p in pendings] == list(range(first, first + 5))


def test_blocking_admin_send_still_returns_a_mined_receipt():
    admin = _admin()
    user = Account.create().address
    before = admin.usd_balance(user)
    receipt = admin.faucet_drip(user)
    assert receipt["status"] == 1
    assert admin.usd_balance(user) > before


def test_fund_gas_sends_native_tokens():
    admin = _admin()
    user = Account.create().address
    receipt = admin.fund_gas(user, 10**15)
    assert receipt["status"] == 1
    assert admin.native_balance(user) == 10**15


def test_receipts_batch_formats_and_reports_unmined():
    admin = _admin()
    client = admin._client  # noqa: SLF001
    receipt = client.admin_sender.send_value(client.admin.address, 0, timeout=30)
    rpc = client.admin_sender._rpc  # noqa: SLF001
    got = rpc.receipts([bytes(receipt["transactionHash"]), b"\x11" * 32])
    assert got[0]["status"] == 1 and isinstance(got[0]["blockNumber"], int)
    assert got[1] is None


def test_read_market_states_batches_slots_and_registry():
    admin = _admin()
    unknown_cid = secrets.token_bytes(32)
    states = admin.read_market_states([(unknown_cid, [1, 2])] * 3)
    assert states == [(0, 0, 0)] * 3


def _admin_without_env_file() -> OnchainAdmin:
    settings = Settings(_env_file=None)
    d = Deployment.load(settings.deployment_path)
    client = Web3Client(settings, d)
    return OnchainAdmin(client, Contracts(client.web3, d))


def test_sync_chunk_size_leaves_half_the_capacity_to_user_trades(monkeypatch):
    # Two transactions per market: a chunk may fill at most half the slots.
    # The default of 64, whatever a developer's env or .env says.
    monkeypatch.delenv("AGENTPIT_ADMIN_TX_MAX_IN_FLIGHT", raising=False)
    assert _admin_without_env_file().sync_chunk_size == 16

    monkeypatch.setenv("AGENTPIT_ADMIN_TX_MAX_IN_FLIGHT", "1")
    assert _admin_without_env_file().sync_chunk_size == 1
