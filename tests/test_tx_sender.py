"""AdminTxSender against a fake skaled: nonces counted locally, many txs in
flight, receipts batch-polled, slots bounded."""

import threading
import time

import pytest
from eth_account import Account
from web3.exceptions import TimeExhausted

from agentpit.onchain.tx_sender import TRANSFER_GAS, AdminTxSender, PendingTx
from tests.fake_skaled import (
    CHAIN_ID,
    FakeClock,
    FakeFn as _Fn,
    FakeRpcError,
    FakeSkaled,
)
from tests.fake_skaled import make_sender as _sender


def test_submits_consecutive_nonces_without_waiting():
    chain = FakeSkaled()
    sender, account, _ = _sender(chain)
    chain.committed[account.address] = 5

    pendings = [sender.submit(_Fn()) for _ in range(3)]

    assert [p.nonce for p in pendings] == [5, 6, 7]
    assert chain.block == 0  # nothing waited for a block
    assert chain.nonce_calls == 1  # seeded once, then counted locally
    receipts = sender.wait_all(pendings, timeout=10)
    assert [r["status"] for r in receipts] == [1, 1, 1]
    assert len(set(chain.blocks_of(pendings))) == 1  # one block


def test_send_returns_the_receipt_whatever_its_status():
    chain = FakeSkaled()
    sender, account, _ = _sender(chain)
    chain.revert.add(0)
    receipt = sender.send(_Fn(), timeout=10)
    assert receipt["status"] == 0


def test_wait_times_out_when_nothing_mines():
    chain = FakeSkaled()
    sender, _, _ = _sender(chain, mine_on_sleep=False)
    pending = sender.submit(_Fn())
    with pytest.raises(TimeExhausted):
        sender.wait(pending, timeout=2)


def test_in_flight_is_bounded():
    chain = FakeSkaled()
    sender, _, _ = _sender(chain, max_in_flight=2)
    first = [sender.submit(_Fn()) for _ in range(2)]
    assert chain.block == 0
    third = sender.submit(_Fn())  # no free slot: waits for a block first
    assert chain.block >= 1
    assert third.nonce == 2
    sender.wait_all(first + [third], timeout=10)


def test_estimate_gets_the_buffer_and_a_static_limit_skips_it():
    chain = FakeSkaled()
    sender, _, _ = _sender(chain)
    sender.submit(_Fn(), gas_buffer_pct=20)
    assert chain.accepted[-1]["tx"]["gas"] == 60_000  # 50_000 estimate + 20%
    sender.submit(_Fn(), gas=120_000)
    assert chain.accepted[-1]["tx"]["gas"] == 120_000
    assert chain.estimate_calls == 1


def test_estimate_failure_with_txs_in_flight_waits_for_them_and_retries():
    chain = FakeSkaled()
    sender, _, _ = _sender(chain)
    first = sender.submit(_Fn())
    chain.estimate_errors.append(FakeRpcError("execution reverted"))
    second = sender.submit(_Fn())  # first had to mine before the re-estimate
    assert chain.mined.get(first.tx_hash) is not None
    assert second.nonce == first.nonce + 1


def test_failed_estimate_waits_only_for_txs_already_in_flight():
    chain = FakeSkaled()
    clock = FakeClock()
    account = Account.create()
    late = {"started": False, "pending": None}

    def sleep(seconds):
        clock.advance(seconds)
        if not late["started"]:
            # A tx that arrives while the estimate is waiting, and never mines.
            # It was not in flight when the estimate failed, so it must not be
            # waited for.
            late["started"] = True
            chain.lose.add(1)
            late["pending"] = sender.submit(_Fn(), gas=100_000)
        chain.mine()

    sender = AdminTxSender(chain, account, CHAIN_ID, clock=clock, sleep=sleep)
    first = sender.submit(_Fn())
    chain.estimate_errors.append(FakeRpcError("execution reverted"))
    started = clock()

    second = sender.submit(_Fn())

    assert chain.mined.get(first.tx_hash) is not None  # waited for the snapshot
    assert late["pending"] is not None and late["pending"].nonce == 1
    assert chain.mined.get(late["pending"].tx_hash) is None  # still in flight
    assert second.nonce == 2
    assert chain.estimate_calls == 3  # first tx, failed estimate, re-estimate
    assert clock() - started < 5  # not the 60 s drain bound


def test_estimate_failure_with_nothing_in_flight_raises():
    chain = FakeSkaled()
    sender, _, _ = _sender(chain)
    chain.estimate_errors.append(FakeRpcError("execution reverted"))
    with pytest.raises(FakeRpcError):
        sender.submit(_Fn())


def test_value_transfer_uses_21000_gas_and_no_estimate():
    chain = FakeSkaled()
    sender, _, _ = _sender(chain)
    to = Account.create().address
    receipt = sender.send_value(to, 123, timeout=10)
    assert receipt["status"] == 1
    tx = chain.accepted[-1]["tx"]
    # Public: `UserGasSponsor` books each top-up it sends at this figure.
    assert TRANSFER_GAS == 21_000
    assert tx["gas"] == TRANSFER_GAS and tx["value"] == 123
    assert chain.estimate_calls == 0


def test_fees_are_cached():
    chain = FakeSkaled()
    sender, _, _ = _sender(chain)
    for _ in range(5):
        sender.submit(_Fn(), gas=100_000)
    assert chain.fee_calls == 1
    tx = chain.accepted[-1]["tx"]
    assert tx["maxFeePerGas"] == 200_000 and tx["maxPriorityFeePerGas"] == 0


def test_concurrent_senders_share_blocks():
    chain = FakeSkaled()
    account = Account.create()
    sender = AdminTxSender(chain, account, CHAIN_ID, poll_interval=0.005)
    stop = threading.Event()

    def miner():
        while not stop.is_set():
            time.sleep(0.02)
            chain.mine()

    t = threading.Thread(target=miner, daemon=True)
    t.start()
    receipts = []
    lock = threading.Lock()

    def worker():
        r = sender.send(_Fn(), timeout=10, gas=100_000)
        with lock:
            receipts.append(r)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    stop.set()
    t.join()

    assert sorted(r["nonce"] for r in receipts) == list(range(20))
    blocks = [r["blockNumber"] for r in receipts]
    assert len(set(blocks)) < len(blocks)  # some block carried several


def test_queued_submitters_share_one_slot_deadline():
    chain = FakeSkaled()  # never mined: the one slot stays taken
    sender = AdminTxSender(
        chain,
        Account.create(),
        CHAIN_ID,
        max_in_flight=1,
        slot_timeout=0.5,
        poll_interval=0.01,
    )
    sender.submit(_Fn(), gas=100_000)
    outcomes = []
    lock = threading.Lock()

    def worker():
        try:
            sender.submit(_Fn(), gas=100_000)
        except Exception as exc:
            with lock:
                outcomes.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    started = time.monotonic()
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    elapsed = time.monotonic() - started

    assert len(outcomes) == 3
    assert all(isinstance(exc, TimeExhausted) for exc in outcomes)
    assert elapsed < 1.2  # one shared 0.5 s deadline, not 3 x 0.5 s in a row
    assert len(chain.accepted) == 1  # no refused submitter consumed a nonce


# --- a value send with its own, shorter wait for a slot ---------------------
#
# A gas top-up runs while the user's lock is held, so `OnchainAdmin.fund_gas`
# bounds the whole send by the sponsor's timeout instead of letting the slot
# wait take the sender's own 120 s first.


def _full_pipeline(**kw):
    """A sender whose two slots are taken by transfers that never mine."""
    chain = FakeSkaled()
    sender, _, clock = _sender(chain, max_in_flight=2, mine_on_sleep=False, **kw)
    to = Account.create().address
    sender.submit_value(to, 1)
    sender.submit_value(to, 1)
    return chain, sender, clock, to


def test_a_value_send_can_cap_its_own_slot_wait():
    chain, sender, clock, to = _full_pipeline(slot_timeout=120)
    started = clock()
    with pytest.raises(TimeExhausted, match="after 5s"):
        sender.submit_value(to, 1, slot_timeout=5)
    assert 5 <= clock() - started < 6  # not the sender's 120 s
    assert len(chain.accepted) == 2  # the refused submit took no nonce


def test_without_a_slot_timeout_the_senders_own_applies():
    chain, sender, clock, to = _full_pipeline(slot_timeout=7)
    started = clock()
    with pytest.raises(TimeExhausted, match="after 7s"):
        sender.submit_value(to, 1)
    assert 7 <= clock() - started < 8


def test_send_value_stops_waiting_for_a_slot_at_its_slot_timeout():
    chain, sender, clock, to = _full_pipeline(slot_timeout=120)
    started = clock()
    with pytest.raises(TimeExhausted, match="free admin transaction slot"):
        sender.send_value(to, 1, timeout=30, slot_timeout=30)
    assert clock() - started < 31  # not 120 s of slot wait and then 30 s more
    assert len(chain.accepted) == 2


def test_the_slot_wait_counts_against_a_value_sends_timeout():
    """A slot that frees after 20 s leaves 10 s of the 30 for the receipt, and
    nothing mines after that, so the call gives up at 30 s, not at 50."""
    chain = FakeSkaled()
    clock = FakeClock()
    started = clock()
    mined: list[int] = []

    def sleep(seconds):
        clock.advance(seconds)
        if not mined and clock() - started >= 20:
            mined.append(chain.mine())  # the first transfer lands, once

    sender = AdminTxSender(
        chain,
        Account.create(),
        CHAIN_ID,
        clock=clock,
        sleep=sleep,
        max_in_flight=1,
        slot_timeout=120,
    )
    to = Account.create().address
    sender.submit_value(to, 1)  # takes the one slot

    with pytest.raises(TimeExhausted, match="not in the chain"):
        sender.send_value(to, 2, timeout=30, slot_timeout=30)

    assert 29 <= clock() - started <= 31
    assert len(chain.accepted) == 2  # the slot freed, so the transfer did go out


def test_a_value_send_with_a_slot_timeout_still_returns_its_receipt():
    chain = FakeSkaled()
    sender, _, _ = _sender(chain)
    to = Account.create().address
    receipt = sender.send_value(to, 123, timeout=10, slot_timeout=10)
    assert receipt["status"] == 1
    assert chain.accepted[-1]["tx"]["value"] == 123


def test_pending_tx_is_hashable_value():
    p = PendingTx(tx_hash=b"\x01" * 32, nonce=3)
    assert p == PendingTx(tx_hash=b"\x01" * 32, nonce=3)
