"""AdminTxSender.submit_many: a whole chunk of admin transactions in one
JSON-RPC batch, with the single path's safety rules: an action whose
broadcast the node may hold is never signed on a second nonce."""

import threading
import time

import pytest
import requests
from eth_account import Account
from eth_utils import keccak
from hexbytes import HexBytes
from web3 import Web3
from web3.exceptions import RequestTimedOut, TimeExhausted

from agentpit.onchain.tx_sender import (
    _SEND_BATCH,
    AdminTxSender,
    BatchRefused,
    BatchUnanswered,
    PendingTx,
    SendError,
    Web3ChainRpc,
    classify_send_error,
    failed_before_connecting,
)
from tests.fake_skaled import CHAIN_ID, FakeFn, FakeRpcError, FakeSkaled
from tests.fake_skaled import make_sender as _sender
from tests.test_tx_sender_recovery import (
    _http_error,
    _refused,
    _reset_after_connecting,
)

_FEE_LOW = "Transaction gas price lower than current eth_gasPrice."
_SAME_NONCE = (
    "Pending transaction with same nonce already exists (skale: we ignore gas price)."
)


class _Call:
    """A contract call whose calldata carries its tag, so a test can count
    how many times each action ran."""

    address = FakeFn.address

    def __init__(self, tag: int):
        self.tag = tag

    def build_transaction(self, tx):
        data = "0x" + self.tag.to_bytes(4, "big").hex()
        return {**tx, "to": self.address, "data": data, "value": 0}


def _calls(n: int, start: int = 0, gas: int = 100_000):
    return [(_Call(tag), gas) for tag in range(start, start + n)]


def _tag(item: dict) -> int | None:
    tx = item["tx"]
    if Web3.to_checksum_address(tx["to"]) != _Call.address:
        return None  # a filler or a transfer
    return int.from_bytes(HexBytes(tx["data"]), "big")


def _executions(chain: FakeSkaled) -> list[int]:
    """The tag of every call that mined, once per execution."""
    return sorted(
        t
        for item in chain.accepted
        if item["hash"] in chain.mined and (t := _tag(item)) is not None
    )


def _tag_of(chain: FakeSkaled, pending: PendingTx) -> int | None:
    (item,) = [a for a in chain.accepted if a["hash"] == pending.tx_hash]
    return _tag(item)


def _fillers(chain: FakeSkaled, account) -> list[dict]:
    return [
        a
        for a in chain.accepted
        if Web3.to_checksum_address(a["tx"]["to"]) == account.address
        and a["tx"]["value"] == 0
        and not a["tx"]["data"]
    ]


# --- one batch -------------------------------------------------------------


def test_a_chunk_goes_out_as_one_batch_on_consecutive_nonces():
    chain = FakeSkaled()
    sender, account, _ = _sender(chain, max_in_flight=128)
    chain.committed[account.address] = 7

    results = sender.submit_many(_calls(50))

    assert len(chain.batches) == 1 and len(chain.batches[0]) == 50
    assert all(isinstance(r, PendingTx) for r in results)
    assert [r.nonce for r in results] == list(range(7, 57))
    assert [_tag_of(chain, r) for r in results] == list(range(50))  # call order
    assert chain.block == 0  # nothing waited for a block
    receipts = sender.wait_all(results, timeout=10)
    assert all(r["status"] == 1 for r in receipts)
    assert len(set(chain.blocks_of(results))) == 1  # one block
    assert _executions(chain) == list(range(50))
    assert sender.submit(FakeFn(), gas=100_000).nonce == 57


def test_values_go_out_as_one_batch_of_plain_transfers():
    chain = FakeSkaled()
    sender, _, _ = _sender(chain)
    to = Account.create().address

    results = sender.submit_values([(to, 1), (to, 2), (to, 3)])

    assert len(chain.batches) == 1
    assert [r.nonce for r in results] == [0, 1, 2]
    assert [a["tx"]["value"] for a in chain.accepted] == [1, 2, 3]
    assert all(a["tx"]["gas"] == 21_000 for a in chain.accepted)
    assert chain.estimate_calls == 0
    assert all(r["status"] == 1 for r in sender.wait_all(results, timeout=10))


def test_a_call_that_cannot_be_built_fails_alone():
    class _Broken:
        def build_transaction(self, tx):
            raise ValueError("bad argument")

    chain = FakeSkaled()
    sender, _, _ = _sender(chain)
    results = sender.submit_many(
        [_calls(1)[0], (_Broken(), 100_000), (_Call(5), None), _calls(1, 1)[0]]
    )
    assert isinstance(results[1], ValueError)
    assert isinstance(results[2], ValueError)  # no static gas limit
    assert [results[0].nonce, results[3].nonce] == [0, 1]


def test_batches_hold_at_most_100_and_at_most_max_in_flight():
    assert _SEND_BATCH == 100  # under SKALE's cap of 128 requests per batch
    chain = FakeSkaled()
    sender, _, _ = _sender(chain, max_in_flight=256)
    results = sender.submit_many(_calls(250))
    assert [len(b) for b in chain.batches] == [100, 100, 50]
    assert [r.nonce for r in results] == list(range(250))

    chain = FakeSkaled()
    sender, _, _ = _sender(chain, max_in_flight=8)
    results = sender.submit_many(_calls(17))
    assert [len(b) for b in chain.batches] == [8, 8]  # the 17th went alone
    assert sorted(r.nonce for r in results) == list(range(17))
    sender.wait_all(results, timeout=30)
    assert _executions(chain) == list(range(17))


def test_a_batch_waits_until_every_one_of_its_items_has_a_slot():
    chain = FakeSkaled()
    sender, _, _ = _sender(chain, max_in_flight=6)
    first = sender.submit_many(_calls(4))
    assert chain.block == 0
    second = sender.submit_many(_calls(4, 4))  # 2 slots free: needs a block
    assert chain.block >= 1
    assert [len(b) for b in chain.batches] == [4, 4]
    sender.wait_all(first + second, timeout=10)


def test_no_free_slots_fails_every_item_without_sending():
    chain = FakeSkaled()
    sender, _, _ = _sender(chain, max_in_flight=4, slot_timeout=2, mine_on_sleep=False)
    sender.submit_many(_calls(4))
    results = sender.submit_many(_calls(4, 4))
    assert all(isinstance(r, TimeExhausted) for r in results)
    assert len(chain.batches) == 1  # nothing was sent
    chain.mine()
    assert sender.submit(FakeFn(), gas=100_000).nonce == 4  # no nonce consumed


def test_one_in_flight_sends_each_call_on_its_own():
    chain = FakeSkaled()
    sender, _, _ = _sender(chain, max_in_flight=1)
    results = sender.submit_many(_calls(3))
    assert chain.batches == []
    assert [r.nonce for r in results] == [0, 1, 2]
    sender.wait_all(results, timeout=10)
    assert _executions(chain) == [0, 1, 2]


def test_a_batch_and_single_senders_share_the_nonce_stream():
    chain = FakeSkaled()
    sender = AdminTxSender(chain, Account.create(), CHAIN_ID, poll_interval=0.005)
    stop = threading.Event()

    def miner():
        while not stop.is_set():
            time.sleep(0.02)
            chain.mine()

    t = threading.Thread(target=miner, daemon=True)
    t.start()
    singles: list = []
    lock = threading.Lock()

    def worker(tag):
        r = sender.send(_Call(tag), timeout=10, gas=100_000)
        with lock:
            singles.append(r)

    threads = [threading.Thread(target=worker, args=(1000 + i,)) for i in range(10)]
    for th in threads:
        th.start()
    batch = sender.submit_many(_calls(30))
    for th in threads:
        th.join()
    receipts = sender.wait_all(batch, timeout=10)
    stop.set()
    t.join()

    nonces = sorted([r["nonce"] for r in singles] + [r["nonce"] for r in receipts])
    assert nonces == list(range(40))
    assert _executions(chain) == list(range(30)) + list(range(1000, 1010))


# --- refused items -----------------------------------------------------------


def test_a_refused_item_gets_a_filler_and_is_sent_again_on_a_fresh_nonce():
    chain = FakeSkaled()
    sender, account, _ = _sender(chain)
    chain.refuse[3] = FakeRpcError(_FEE_LOW)
    real = chain.send_raw_batch

    def batch(raws):
        answers = real(raws)
        chain.fee = (400_000, 0)  # the price moved: the filler needs the new one
        return answers

    chain.send_raw_batch = batch

    results = sender.submit_many(_calls(10))

    assert len(chain.batches) == 1
    assert [r.nonce for r in results] == [0, 1, 2, 10, 4, 5, 6, 7, 8, 9]
    (filler,) = _fillers(chain, account)
    assert filler["nonce"] == 3
    assert filler["tx"]["maxFeePerGas"] == 400_000  # a fresh fee
    receipts = sender.wait_all(results, timeout=10)
    assert all(r["status"] == 1 for r in receipts)
    assert _executions(chain) == list(range(10))  # each action ran once


def test_a_nonce_held_by_a_leftover_moves_only_that_action():
    """A restart left a transaction on nonce 0: item 0 is refused "same
    nonce", its filler too, and the action goes out again after the batch."""
    chain = FakeSkaled()
    sender, account, _ = _sender(chain, mine_on_sleep=False)
    chain.inject(account, 0)

    results = sender.submit_many(_calls(5))

    assert [r.nonce for r in results] == [5, 1, 2, 3, 4]
    assert [a["nonce"] for a in chain.accepted] == [0, 1, 2, 3, 4, 5]  # no filler
    chain.mine()
    assert all(r["status"] == 1 for r in sender.wait_all(results, timeout=10))
    assert _executions(chain) == list(range(5))


def test_refused_items_at_the_end_of_a_batch_need_no_filler():
    """Nothing of ours follows them, so their nonces are simply free again,
    exactly as after a refused single send."""
    chain = FakeSkaled()
    sender, account, _ = _sender(chain)
    chain.refuse[3] = FakeRpcError(_FEE_LOW)
    chain.refuse[4] = FakeRpcError(_FEE_LOW)

    results = sender.submit_many(_calls(5))

    assert [r.nonce for r in results] == [0, 1, 2, 3, 4]
    assert _fillers(chain, account) == []
    sender.wait_all(results, timeout=10)
    assert _executions(chain) == list(range(5))


def test_an_item_refused_for_good_returns_its_error():
    chain = FakeSkaled()
    sender, account, _ = _sender(chain)
    broke = FakeRpcError("insufficient funds for gas * price + value")
    chain.refuse[1] = broke
    chain.refuse[3] = broke  # its single resend, after the batch, too

    results = sender.submit_many(_calls(3))

    assert results[1] is broke
    assert [results[0].nonce, results[2].nonce] == [0, 2]
    assert [f["nonce"] for f in _fillers(chain, account)] == [1]
    sender.wait_all([results[0], results[2]], timeout=10)
    assert _executions(chain) == [0, 2]
    assert sender.submit(FakeFn(), gas=100_000).nonce == 3


def test_mtm_off_inside_a_batch_falls_back_to_one_at_a_time():
    chain = FakeSkaled(mtm=False)
    sender, _, _ = _sender(chain, max_in_flight=8)

    results = sender.submit_many(_calls(5))

    assert sender.max_in_flight == 1
    assert [r.nonce for r in results] == [0, 1, 2, 3, 4]
    sender.wait_all(results, timeout=30)
    assert _executions(chain) == list(range(5))
    blocks = chain.blocks_of(results)
    assert blocks == sorted(blocks) and len(set(blocks)) == 5
    more = sender.submit_many(_calls(2, 5))
    assert len(chain.batches) == 1  # later calls skip the batch
    sender.wait_all(more, timeout=30)
    assert _executions(chain) == list(range(7))


# --- unanswered batches ------------------------------------------------------


@pytest.mark.parametrize(
    "lost",
    [
        requests.ConnectionError("reset by peer"),
        BatchUnanswered("3 answers for 8 transactions"),
    ],
)
def test_a_lost_batch_answer_is_settled_by_resending_the_identical_batch(lost):
    chain = FakeSkaled()
    sender, _, _ = _sender(chain)
    chain.batch_accept_then_raise.append(lost)

    results = sender.submit_many(_calls(8))

    assert len(chain.batches) == 2 and chain.batches[0] == chain.batches[1]
    assert [r.nonce for r in results] == list(range(8))
    assert len(chain.accepted) == 8  # one copy each
    sender.wait_all(results, timeout=10)
    assert _executions(chain) == list(range(8))


def test_an_item_the_node_refused_unseen_is_taken_by_the_resend():
    chain = FakeSkaled()
    sender, account, _ = _sender(chain)
    chain.refuse[5] = FakeRpcError(_FEE_LOW)  # its answer is lost with the rest
    chain.batch_accept_then_raise.append(requests.ConnectionError("reset by peer"))

    results = sender.submit_many(_calls(8))

    assert [r.nonce for r in results] == list(range(8))
    assert _fillers(chain, account) == []
    sender.wait_all(results, timeout=10)
    assert _executions(chain) == list(range(8))


def test_a_batch_cut_off_halfway_is_completed_by_the_resend():
    """The node took the first three items, then the connection broke: the
    resend finds those three already queued and takes the rest."""
    chain = FakeSkaled()
    sender, _, _ = _sender(chain)
    real = chain.send_raw_batch
    sent: list[list[bytes]] = []

    def batch(raws):
        sent.append([bytes(r) for r in raws])
        if len(sent) == 1:
            real(raws[:3])
            raise BatchUnanswered("3 answers for 6 sends")
        return real(raws)

    chain.send_raw_batch = batch

    results = sender.submit_many(_calls(6))

    assert sent[0] == sent[1]
    assert [r.nonce for r in results] == list(range(6))
    assert len(chain.accepted) == 6
    sender.wait_all(results, timeout=10)
    assert _executions(chain) == list(range(6))


def test_a_batch_whose_first_copy_never_connected_is_taken_by_the_resend():
    chain = FakeSkaled()
    sender, _, _ = _sender(chain)
    chain.batch_errors.append(_refused())

    results = sender.submit_many(_calls(3))

    assert [r.nonce for r in results] == [0, 1, 2]
    assert len(chain.batches) == 2 and chain.batches[0] == chain.batches[1]
    sender.wait_all(results, timeout=10)
    assert _executions(chain) == [0, 1, 2]


def test_a_batch_that_never_left_the_host_consumes_no_nonce():
    chain = FakeSkaled()
    sender, _, _ = _sender(chain)
    first = _refused()
    chain.batch_errors.extend([first, _refused()])

    results = sender.submit_many(_calls(4))

    assert all(r is first for r in results)
    assert chain.accepted == []
    follow = sender.submit(FakeFn(), gas=100_000)
    assert follow.nonce == 0  # the nonce is reused
    assert sender.wait(follow, timeout=10)["status"] == 1


@pytest.mark.parametrize(
    "make_first, make_second",
    [
        (lambda: requests.ConnectionError("reset by peer"), _reset_after_connecting),
        (_refused, _reset_after_connecting),
        (_reset_after_connecting, _refused),
    ],
)
def test_a_batch_lost_twice_is_unknowable_and_never_signed_again(
    make_first, make_second
):
    chain = FakeSkaled()
    sender, account, _ = _sender(chain, stall_after=5)
    first = make_first()
    chain.batch_errors.extend([first, make_second()])

    results = sender.submit_many(_calls(4))

    assert all(r is first for r in results)  # the FIRST error
    assert len(chain.batches) == 2 and chain.batches[0] == chain.batches[1]
    assert chain.accepted == []
    assert sender._in_flight_hashes() == {  # noqa: SLF001
        keccak(raw) for raw in chain.batches[0]
    }
    follow = sender.submit(_Call(99), gas=100_000)
    assert follow.nonce == 4  # nonces 0-3 counted as used
    assert sender.wait(follow, timeout=60)["status"] == 1  # the healer filled 0-3
    assert sorted(f["nonce"] for f in _fillers(chain, account)) == [0, 1, 2, 3]
    assert _executions(chain) == [99]  # no batch action was signed again


def test_a_batch_taken_but_never_answered_still_runs_once():
    """The node took the batch and neither answer came back: every item is
    unknowable with its hash tracked, and the copies the node holds still
    mine, each once."""
    chain = FakeSkaled()
    sender, _, _ = _sender(chain)
    first = requests.ConnectionError("reset by peer")
    real = chain.send_raw_batch

    def batch(raws):
        if chain.batches:
            chain.batches.append([bytes(r) for r in raws])
            raise requests.ConnectionError("again")  # the resend is lost too
        real(raws)
        raise first  # taken, answer lost

    chain.send_raw_batch = batch

    results = sender.submit_many(_calls(4))

    assert all(r is first for r in results)
    assert sender._in_flight_hashes() == {  # noqa: SLF001
        keccak(raw) for raw in chain.batches[0]
    }
    follow = sender.submit(_Call(99), gas=100_000)
    assert follow.nonce == 4
    assert sender.wait(follow, timeout=10)["status"] == 1
    assert sender._in_flight_hashes() == set()  # noqa: SLF001 - receipts found
    assert _executions(chain) == [0, 1, 2, 3, 99]


def test_a_lost_batch_that_mined_meanwhile_is_found_by_its_receipts():
    """skaled answers "invalid nonce" to the resend of a mined transaction.
    Each such item is looked up by its receipt, never signed again."""
    chain = FakeSkaled()
    sender, _, _ = _sender(chain)
    real = chain.send_raw_batch

    def batch(raws):
        try:
            return real(raws)
        except requests.ConnectionError:
            chain.mine()
            raise

    chain.send_raw_batch = batch
    chain.batch_accept_then_raise.append(requests.ConnectionError("reset by peer"))

    results = sender.submit_many(_calls(4))

    assert [r.nonce for r in results] == [0, 1, 2, 3]
    assert [r.tx_hash for r in results] == [keccak(raw) for raw in chain.batches[0]]
    assert len(chain.accepted) == 4
    assert all(r["status"] == 1 for r in sender.wait_all(results, timeout=10))


def test_a_resend_answered_invalid_nonce_without_our_receipt_is_unknowable():
    chain = FakeSkaled()
    sender, account, _ = _sender(chain)
    first = requests.ConnectionError("reset by peer")
    real = chain.send_raw_batch

    def batch(raws):
        if len(chain.batches) == 0:
            chain.batches.append([bytes(r) for r in raws])
            chain.committed[account.address] = 3  # another writer used 0-2
            raise first
        return real(raws)

    chain.send_raw_batch = batch

    results = sender.submit_many(_calls(4))

    assert all(r is first for r in results[:3])
    assert isinstance(results[3], PendingTx) and results[3].nonce == 3
    assert [a["nonce"] for a in chain.accepted] == [3]  # nothing signed again
    assert sender.submit(FakeFn(), gas=100_000).nonce == 4


def test_a_resend_answered_same_nonce_is_unknowable_not_moved():
    chain = FakeSkaled()
    sender, _, _ = _sender(chain)
    first = requests.ConnectionError("reset by peer")
    chain.batch_errors.append(first)
    chain.refuse[1] = FakeRpcError(_SAME_NONCE)  # the resend's item 1

    results = sender.submit_many(_calls(4))

    assert results[1] is first
    assert [results[i].nonce for i in (0, 2, 3)] == [0, 2, 3]
    assert [a["nonce"] for a in chain.accepted] == [0, 2, 3]  # never moved
    assert sender.submit(FakeFn(), gas=100_000).nonce == 4  # nonce 1 counted


def test_an_item_whose_answer_was_lost_is_resent_identically():
    chain = FakeSkaled()
    sender, _, _ = _sender(chain)
    chain.answer_lost[2] = RequestTimedOut("request timed out")

    results = sender.submit_many(_calls(4))

    assert [r.nonce for r in results] == [0, 1, 2, 3]
    assert chain.batches[1] == [chain.batches[0][2]]  # that item, same bytes
    assert len(chain.accepted) == 4
    sender.wait_all(results, timeout=10)
    assert _executions(chain) == [0, 1, 2, 3]


def test_after_a_lost_batch_the_rest_is_not_sent():
    """A batch nobody answered twice means the node is out of reach: more
    batches would only add unknowable nonces for the healer to fill."""
    chain = FakeSkaled()
    sender, _, _ = _sender(chain, max_in_flight=4)
    first = requests.ConnectionError("reset by peer")
    chain.batch_errors.extend([first, requests.ConnectionError("again")])

    results = sender.submit_many(_calls(10))

    assert len(chain.batches) == 2  # the batch and its resend, nothing more
    assert all(r is first for r in results)


def test_a_batch_refused_as_a_whole_is_sent_one_at_a_time():
    """skaled answers a batch above its cap with one error object and looks at
    no item: no nonce was used, the calls go out one by one."""
    chain = FakeSkaled()
    sender, _, _ = _sender(chain)
    chain.batch_errors.append(
        BatchRefused("{'code': 195948557, 'message': 'too many requests in batch'}")
    )

    results = sender.submit_many(_calls(6))

    assert len(chain.batches) == 1
    assert [r.nonce for r in results] == list(range(6))
    assert len(chain.accepted) == 6
    sender.wait_all(results, timeout=10)
    assert _executions(chain) == list(range(6))


# --- Web3ChainRpc.send_raw_batch ---------------------------------------------


class _Provider:
    def __init__(self, answer):
        self.answer = answer
        self.batches: list[list] = []

    def make_batch_request(self, requests_):
        self.batches.append(list(requests_))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer(requests_) if callable(self.answer) else self.answer


class _W3:
    def __init__(self, answer):
        self.provider = _Provider(answer)


_RAWS = [b"\x01\x02", b"\x03", b"\x04\x05\x06"]
_HASHES = [keccak(r) for r in _RAWS]


def _ok(n: int, tx_hash: bytes) -> dict:
    return {"jsonrpc": "2.0", "id": n, "result": Web3.to_hex(tx_hash)}


def _err(n: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": n, "error": {"code": -32000, "message": message}}


def test_send_raw_batch_answers_each_transaction_in_order():
    w3 = _W3(
        [
            _ok(4, _HASHES[0]),
            _err(
                5, "Same transaction already exists in the pending transaction queue."
            ),
            _err(6, "Invalid transaction nonce."),
        ]
    )
    answers = Web3ChainRpc(w3).send_raw_batch(_RAWS)
    (sent,) = w3.provider.batches
    assert sent == [("eth_sendRawTransaction", [Web3.to_hex(r)]) for r in _RAWS]
    assert answers[0] is None
    assert classify_send_error(answers[1]) is SendError.DUPLICATE
    assert classify_send_error(answers[2]) is SendError.NONCE_INVALID


def test_send_raw_batch_reads_a_timed_out_item_as_unanswered():
    w3 = _W3([_ok(1, _HASHES[0]), _err(2, "request timed out"), _ok(3, _HASHES[2])])
    answers = Web3ChainRpc(w3).send_raw_batch(_RAWS)
    assert classify_send_error(answers[1]) is SendError.TRANSPORT


@pytest.mark.parametrize(
    "answer",
    [
        [_ok(1, _HASHES[0]), _ok(2, _HASHES[1])],  # short
        [_ok(1, _HASHES[0]), _ok(2, _HASHES[2]), _ok(3, _HASHES[1])],  # misplaced
        [_ok(1, _HASHES[0]), _ok(1, _HASHES[1]), _ok(3, _HASHES[2])],  # same id
        [
            _ok(1, _HASHES[0]),
            {"jsonrpc": "2.0", "result": Web3.to_hex(_HASHES[1])},
            _ok(3, _HASHES[2]),
        ],  # no id
        [_ok(1, _HASHES[0]), {"id": 2, "result": None}, _ok(3, _HASHES[2])],
        {"jsonrpc": "2.0", "id": 1, "result": "0x"},  # not a list, not an error
        ValueError("Could not decode b'<html>bad gateway</html>'"),
    ],
)
def test_send_raw_batch_answers_that_cannot_be_placed_read_as_unanswered(answer):
    with pytest.raises(BatchUnanswered) as info:
        Web3ChainRpc(_W3(answer)).send_raw_batch(_RAWS)
    assert classify_send_error(info.value) is SendError.TRANSPORT
    assert not failed_before_connecting(info.value)


def test_send_raw_batch_refused_as_a_whole_raises_a_refusal():
    answer = {
        "jsonrpc": "2.0",
        "id": 0xBADF00D,
        "error": {"code": -32600, "message": "Max number of batch requests is 128"},
    }
    with pytest.raises(BatchRefused) as info:
        Web3ChainRpc(_W3(answer)).send_raw_batch(_RAWS)
    assert classify_send_error(info.value) is not SendError.TRANSPORT
    assert "128" in str(info.value)


def test_send_raw_batch_passes_transport_failures_through():
    refused = _refused()
    with pytest.raises(requests.ConnectionError) as info:
        Web3ChainRpc(_W3(refused)).send_raw_batch(_RAWS)
    assert info.value is refused
    assert failed_before_connecting(info.value)


@pytest.mark.parametrize(
    "status, kind", [(502, SendError.TRANSPORT), (429, SendError.OTHER)]
)
def test_send_raw_batch_passes_http_failures_through(status, kind):
    """A proxy's 5xx may come after the node took the batch (no answer); a
    429 means it never got there (refused as a whole)."""
    error = _http_error(status)
    with pytest.raises(requests.HTTPError) as info:
        Web3ChainRpc(_W3(error)).send_raw_batch(_RAWS)
    assert info.value is error
    assert classify_send_error(info.value) is kind
