from types import SimpleNamespace

import pytest
from web3 import Web3

from agentpit.domain.exceptions import AdminGasPausedError
from agentpit.onchain.chain_rpc import Web3ChainRpc
from tests.fake_skaled import FakeFn, FakeSkaled, make_sender

PRICE = 200_000          # FakeSkaled.fee[0]


def _paused_sender(**kw):
    chain = FakeSkaled()
    sender, account, _ = make_sender(chain, alarm_gas=1_000, stop_gas=100, **kw)
    chain.balances[account.address] = 99 * PRICE          # below 100 gas of stop
    sender.refresh_gas_balance()
    return chain, sender, account


def test_no_thresholds_means_never_paused():
    chain = FakeSkaled()
    sender, account, _ = make_sender(chain)
    chain.balances[account.address] = 0
    sender.refresh_gas_balance()
    sender.check_sponsored()
    sender.wait(sender.submit(FakeFn()), timeout=5)


def test_unknown_balance_is_allowed():
    chain = FakeSkaled()
    sender, _account, _ = make_sender(chain, stop_gas=10**9)
    assert sender.gas_state() == "unknown"
    sender.wait(sender.submit(FakeFn()), timeout=5)


def test_states_follow_the_balance():
    chain = FakeSkaled()
    sender, account, _ = make_sender(chain, alarm_gas=1_000, stop_gas=100)
    for balance, state in ((2_000 * PRICE, "ok"), (500 * PRICE, "low"), (50 * PRICE, "paused")):
        chain.balances[account.address] = balance
        sender.refresh_gas_balance()
        assert sender.gas_state() == state


def test_paused_refuses_every_sponsored_send_before_broadcasting():
    chain, sender, account = _paused_sender()
    with pytest.raises(AdminGasPausedError):
        sender.submit(FakeFn())
    with pytest.raises(AdminGasPausedError):
        sender.submit_value(account.address, 1)
    with pytest.raises(AdminGasPausedError):
        sender.submit_many([(FakeFn(), 50_000)])
    with pytest.raises(AdminGasPausedError):
        sender.submit_values([(account.address, 1)])
    with pytest.raises(AdminGasPausedError):
        sender.send_value(account.address, 1, timeout=5)
    with pytest.raises(AdminGasPausedError):
        sender.check_sponsored()
    assert chain.accepted == []


def test_essential_sends_still_go_out_while_paused():
    chain, sender, _account = _paused_sender()
    sender.wait(sender.submit(FakeFn(), essential=True), timeout=5)
    results = sender.submit_many([(FakeFn(), 50_000)], essential=True)
    sender.wait_all(results, timeout=5)
    assert len(chain.accepted) == 2


def test_send_and_send_value_forward_essential():
    chain, sender, account = _paused_sender()
    sender.send(FakeFn(), timeout=5, essential=True)
    sender.send_value(account.address, 1, timeout=5, essential=True)
    assert len(chain.accepted) == 2
    with pytest.raises(AdminGasPausedError):
        sender.send(FakeFn(), timeout=5)
    assert len(chain.accepted) == 2


def test_receipts_debit_the_cached_balance():
    chain = FakeSkaled()
    sender, account, _ = make_sender(chain, stop_gas=100)
    gas_limit = 60_000                                  # 50_000 estimate + 20%
    chain.balances[account.address] = (100 + gas_limit) * PRICE - 1   # 1 wei short after one tx
    sender.refresh_gas_balance()
    sender.wait(sender.submit(FakeFn()), timeout=5)     # still ok; costs gas_limit * PRICE
    with pytest.raises(AdminGasPausedError):
        sender.submit(FakeFn())


def test_debit_ignores_receipts_without_gas_fields():
    chain = FakeSkaled()
    sender, account, _ = make_sender(chain, stop_gas=100)
    chain.balances[account.address] = 10**18
    sender.refresh_gas_balance()
    sender._debit({"transactionHash": b"x"})            # no gasUsed / effectiveGasPrice
    assert sender.gas_state() == "ok"
    assert sender._admin_balance == 10**18               # counted as 0, not an error


def test_web3_chain_rpc_reads_the_checksummed_balance():
    seen = []
    w3 = SimpleNamespace(eth=SimpleNamespace(get_balance=lambda a: seen.append(a) or 42))
    addr = "0x" + "ab" * 20
    assert Web3ChainRpc(w3).balance(addr) == 42   # type: ignore[arg-type]
    assert seen == [Web3.to_checksum_address(addr)]
