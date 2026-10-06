"""AdminTxSender recovers from every way skaled can refuse, half-answer or
lose an admin transaction, without ever sending the same action twice."""

import logging
import socket
import threading

import pytest
import requests
from eth_account import Account
from hexbytes import HexBytes
from urllib3.exceptions import (
    ConnectTimeoutError,
    MaxRetryError,
    NameResolutionError,
    NewConnectionError,
    ProtocolError,
    ReadTimeoutError,
)
from web3 import Web3

from agentpit.onchain.tx_sender import (
    _OWN_RECEIPT_LOOKUPS,
    AdminTxSender,
    PendingTx,
    SendError,
    TxDropped,
    Web3ChainRpc,
    classify_send_error,
    failed_before_connecting,
)
from agentpit.onchain.web3_client import build_http_provider
from tests.fake_skaled import (
    CHAIN_ID,
    FakeClock,
    FakeFn as _Fn,
    FakeRpcError,
    FakeSkaled,
)
from tests.fake_skaled import make_sender as _sender


class _LaggingSkaled(FakeSkaled):
    """Receipts of the `hidden` hashes read as not mined, as on a node whose
    receipt index trails its nonce."""

    def __init__(self):
        super().__init__()
        self.hidden: set[bytes] = set()
        self.lag_reads = 0  # the next N receipts that exist read as not mined

    def receipts(self, tx_hashes):
        out = []
        for h, r in zip(tx_hashes, super().receipts(tx_hashes)):
            if r is not None and self.lag_reads > 0:
                self.lag_reads -= 1
                r = None
            out.append(None if bytes(h) in self.hidden else r)
        return out


class _StaleSkaled(_LaggingSkaled):
    """The next nonce reads answer `stale_reads` first, as when SKALE's proxy
    serves the read from a node a block behind the one that took the send."""

    def __init__(self):
        super().__init__()
        self.stale_reads: list[int] = []

    def nonce(self, address, block):
        if self.stale_reads:
            return self.stale_reads.pop(0)
        return super().nonce(address, block)


@pytest.mark.parametrize(
    "message, kind",
    [
        (
            "Same transaction already exists in the pending transaction queue.",
            SendError.DUPLICATE,
        ),
        ("Transaction is already in the blockchain.", SendError.DUPLICATE),
        ("already known", SendError.DUPLICATE),
        ("transaction already imported", SendError.DUPLICATE),
        (
            "Pending transaction with same nonce already exists (skale: we ignore gas price).",
            SendError.NONCE_TAKEN,
        ),
        ("replacement transaction underpriced", SendError.NONCE_TAKEN),
        ("Invalid transaction nonce.", SendError.NONCE_INVALID),
        ("nonce too low", SendError.NONCE_INVALID),
        ("Transaction gas price lower than current eth_gasPrice.", SendError.FEE_LOW),
        ("max fee per gas less than block base fee", SendError.FEE_LOW),
        ("insufficient funds for gas * price + value", SendError.OTHER),
    ],
)
def test_classify_node_answers(message, kind):
    assert classify_send_error(FakeRpcError(message)) is kind


def test_classify_transport_errors():
    assert classify_send_error(requests.ConnectionError()) is SendError.TRANSPORT
    assert classify_send_error(requests.Timeout()) is SendError.TRANSPORT
    assert classify_send_error(TimeoutError()) is SendError.TRANSPORT


def _http_error(status: int) -> requests.HTTPError:
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f"{status} error", response=response)


@pytest.mark.parametrize(
    "exc, kind",
    [
        # A proxy answer or a cut body: the request may have reached the node.
        (_http_error(502), SendError.TRANSPORT),
        (_http_error(504), SendError.TRANSPORT),
        (requests.exceptions.ChunkedEncodingError("cut"), SendError.TRANSPORT),
        # Rate limited: the node was never asked.
        (_http_error(429), SendError.OTHER),
    ],
)
def test_classify_http_failures(exc, kind):
    assert classify_send_error(exc) is kind


# The shapes requests 2.33 / urllib3 2.6 give a failed POST (checked against
# real sockets in test_connect_phase_is_told_apart_on_real_sockets).
def _refused() -> requests.ConnectionError:
    return requests.exceptions.ConnectionError(
        MaxRetryError(
            None, "/", NewConnectionError(None, "Failed to establish a new connection")
        )
    )


def _connect_timeout() -> requests.ConnectionError:
    return requests.exceptions.ConnectTimeout(
        MaxRetryError(None, "/", ConnectTimeoutError(None, "connect timed out"))
    )


def _unresolved() -> requests.ConnectionError:
    return requests.exceptions.ConnectionError(
        MaxRetryError(
            None, "/", NameResolutionError("rpc.invalid", None, "no such host")
        )
    )


def _reset_after_connecting() -> requests.ConnectionError:
    return requests.exceptions.ConnectionError(
        ProtocolError(
            "Connection aborted.", ConnectionResetError(54, "Connection reset by peer")
        )
    )


def _reset_while_handling_a_refusal() -> requests.ConnectionError:
    """A reset raised while an earlier refusal was being handled: the refusal
    is in its `__context__`, but this request did reach the node."""
    exc = _reset_after_connecting()
    exc.__context__ = _refused()
    return exc


def _bare_error_while_handling_a_refusal() -> requests.ConnectionError:
    exc = requests.ConnectionError("reset by peer")
    exc.__context__ = _refused()
    return exc


def _raised_from_a_refusal() -> requests.ConnectionError:
    exc = requests.ConnectionError("could not connect")
    exc.__cause__ = _refused().args[0]
    return exc


@pytest.mark.parametrize(
    "exc, before_connecting",
    [
        (_refused(), True),
        (_raised_from_a_refusal(), True),
        (_bare_error_while_handling_a_refusal(), False),
        (_connect_timeout(), True),
        (_unresolved(), True),
        (_reset_after_connecting(), False),
        (_reset_while_handling_a_refusal(), False),
        (
            requests.exceptions.ReadTimeout(
                ReadTimeoutError(None, "/", "Read timed out.")
            ),
            False,
        ),
        (requests.ConnectionError("reset by peer"), False),
        (ConnectionRefusedError(61, "refused"), False),  # not from requests
        (TimeoutError(), False),
    ],
)
def test_failed_before_connecting(exc, before_connecting):
    assert classify_send_error(exc) is SendError.TRANSPORT
    assert failed_before_connecting(exc) is before_connecting


def _send_through_web3(url: str) -> Exception:
    provider = build_http_provider(url)
    provider._request_kwargs = {"timeout": 5}  # noqa: SLF001
    with pytest.raises(Exception) as info:
        provider.make_request("eth_sendRawTransaction", ["0x00"])
    return info.value


def test_connect_phase_is_told_apart_on_real_sockets():
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    closed_port = closed.getsockname()[1]
    closed.close()  # nothing listens: the connect is refused
    assert failed_before_connecting(
        _send_through_web3(f"http://127.0.0.1:{closed_port}")
    )

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def take_then_hang_up():
        conn, _ = server.accept()
        conn.recv(65536)  # the request reached "the node"
        conn.close()

    thread = threading.Thread(target=take_then_hang_up, daemon=True)
    thread.start()
    try:
        exc = _send_through_web3(f"http://127.0.0.1:{server.getsockname()[1]}")
    finally:
        thread.join(5)
        server.close()
    assert classify_send_error(exc) is SendError.TRANSPORT
    assert not failed_before_connecting(exc)


def test_duplicate_answer_counts_as_accepted():
    chain = FakeSkaled()
    sender, _, _ = _sender(chain)
    chain.accept_then_raise.append(
        FakeRpcError(
            "Same transaction already exists in the pending transaction queue."
        )
    )
    receipt = sender.send(_Fn(), timeout=10, gas=100_000)
    assert receipt["status"] == 1
    assert len(chain.accepted) == 1  # sent once


def test_taken_nonce_skips_forward():
    """After a restart the previous process's tx may still hold the nonce."""
    chain = FakeSkaled()
    sender, account, _ = _sender(chain)
    chain.inject(account, 0)  # queued by "the previous process"
    pending = sender.submit(_Fn(), gas=100_000)
    assert pending.nonce == 1
    assert sender.wait(pending, timeout=10)["status"] == 1


def test_counter_behind_the_chain_resyncs():
    chain = FakeSkaled()
    sender, account, _ = _sender(chain)
    sender.send(_Fn(), timeout=10, gas=100_000)  # nonce 0, counter now 1
    chain.committed[account.address] = 3  # another writer mined nonces 1 and 2
    pending = sender.submit(_Fn(), gas=100_000)
    assert pending.nonce == 3


def test_mtm_off_falls_back_to_one_in_flight():
    chain = FakeSkaled(mtm=False)
    sender, _, _ = _sender(chain, max_in_flight=8)
    first = sender.submit(_Fn(), gas=100_000)
    second = sender.submit(
        _Fn(), gas=100_000
    )  # refused once, then sent after `first` mined
    assert sender.max_in_flight == 1
    assert second.nonce == first.nonce + 1
    sender.wait_all([first, second], timeout=10)
    blocks = chain.blocks_of([first, second])
    assert blocks[0] < blocks[1]


def test_fee_too_low_refreshes_fees_once():
    chain = FakeSkaled()
    sender, _, _ = _sender(chain)
    sender.submit(_Fn(), gas=100_000)
    chain.send_errors.append(
        FakeRpcError("Transaction gas price lower than current eth_gasPrice.")
    )
    chain.fee = (400_000, 0)
    sender.submit(_Fn(), gas=100_000)
    assert chain.accepted[-1]["tx"]["maxFeePerGas"] == 400_000


def test_lost_answer_but_node_has_it_is_accepted():
    chain = FakeSkaled()
    sender, _, _ = _sender(chain)
    chain.accept_then_raise.append(requests.ConnectionError("reset by peer"))
    receipt = sender.send(_Fn(), timeout=10, gas=100_000)
    assert receipt["status"] == 1
    assert len(chain.accepted) == 1  # the resend was a duplicate, not a second tx


def test_lost_answer_and_node_lacks_it_is_resent():
    chain = FakeSkaled()
    sender, _, _ = _sender(chain)
    chain.send_errors.append(requests.ConnectionError("reset by peer"))
    pending = sender.submit(_Fn(), gas=100_000)
    assert pending.nonce == 0
    assert len(chain.accepted) == 1
    assert sender.wait(pending, timeout=10)["status"] == 1


def test_resend_is_the_identical_signed_transaction():
    chain = FakeSkaled()
    sender, _, _ = _sender(chain)
    sent = []
    real = chain.send_raw

    def spy(raw):
        sent.append(bytes(raw))
        return real(raw)

    chain.send_raw = spy
    chain.send_errors.append(requests.ConnectionError("reset by peer"))
    sender.submit(_Fn(), gas=100_000)
    assert len(sent) == 2
    assert sent[0] == sent[1]


def test_resend_answered_with_a_refusal_is_unknowable_not_resigned():
    """Once a send went unanswered the action never moves to another nonce:
    the node may hold the first copy. Only the same nonce is safe."""
    chain = FakeSkaled()
    sender, _, _ = _sender(chain)
    first = requests.ConnectionError("reset by peer")
    chain.send_errors.extend(
        [first, FakeRpcError("insufficient funds for gas * price + value")]
    )
    with pytest.raises(requests.ConnectionError) as info:
        sender.submit(_Fn(), gas=100_000)
    assert info.value is first
    assert chain.accepted == []  # nothing was signed again
    assert sender.submit(_Fn(), gas=100_000).nonce == 1  # nonce 0 counted as used


def test_resend_answered_nonce_taken_skips_forward():
    chain = FakeSkaled()
    sender, _, _ = _sender(chain)
    chain.send_errors.extend(
        [
            requests.ConnectionError("reset by peer"),
            FakeRpcError(
                "Pending transaction with same nonce already exists "
                "(skale: we ignore gas price)."
            ),
        ]
    )
    pending = sender.submit(_Fn(), gas=100_000)
    assert pending.nonce == 1
    assert len(chain.accepted) == 1


def test_both_sends_unanswered_raise_the_first_error_and_count_the_nonce():
    chain = FakeSkaled()
    sender, _, _ = _sender(chain)
    first = requests.ConnectionError("first")
    chain.send_errors.extend([first, requests.ConnectionError("second")])
    with pytest.raises(requests.ConnectionError) as info:
        sender.submit(_Fn(), gas=100_000)
    assert info.value is first
    assert sender.submit(_Fn(), gas=100_000).nonce == 1


@pytest.mark.parametrize(
    "make_first, make_second",
    [
        (_refused, _refused),
        (_connect_timeout, _refused),
        (_unresolved, _connect_timeout),
    ],
)
def test_connect_failures_on_send_and_resend_leave_the_nonce_free(
    make_first, make_second
):
    """Neither copy ever reached the node: refused, not unknowable. The nonce
    is not counted, so no gap is left for the stall healer to fill."""
    chain = FakeSkaled()
    sender, _, _ = _sender(chain)
    first = make_first()
    chain.send_errors.extend([first, make_second()])
    with pytest.raises(requests.ConnectionError) as info:
        sender.submit(_Fn(), gas=100_000)
    assert info.value is first
    assert chain.accepted == []
    follow = sender.submit(_Fn(), gas=100_000)
    assert follow.nonce == 0  # the nonce is reused
    assert sender.wait(follow, timeout=10)["status"] == 1
    assert len(chain.accepted) == 1  # no filler was needed


@pytest.mark.parametrize(
    "make_first, make_second",
    [(_refused, _reset_after_connecting), (_reset_after_connecting, _refused)],
)
def test_one_copy_possibly_delivered_still_counts_the_nonce(make_first, make_second):
    chain = FakeSkaled()
    sender, _, _ = _sender(chain)
    first = make_first()
    chain.send_errors.extend([first, make_second()])
    with pytest.raises(requests.ConnectionError) as info:
        sender.submit(_Fn(), gas=100_000)
    assert info.value is first
    assert sender.submit(_Fn(), gas=100_000).nonce == 1  # nonce 0 counted as used


def _answer_lost_after_mining(chain):
    """Make the node accept the next send and mine it, while its answer is
    lost: the resend then meets a nonce already used, by this very hash."""
    sent = []
    real = chain.send_raw

    def send_raw(raw):
        sent.append(bytes(raw))
        try:
            real(raw)
        except requests.ConnectionError:
            chain.mine()
            raise

    chain.send_raw = send_raw
    chain.accept_then_raise.append(requests.ConnectionError("reset by peer"))
    return sent


def test_lost_answer_of_a_mined_tx_is_accepted_not_resigned():
    """skaled answers "Invalid transaction nonce" to the resend of a mined
    transaction. Resigning on a new nonce would run the action twice."""
    chain = FakeSkaled()
    sender, _, _ = _sender(chain)
    sent = _answer_lost_after_mining(chain)
    pending = sender.submit(_Fn(), gas=100_000)
    assert pending.nonce == 0
    assert pending.tx_hash == chain.accepted[0]["hash"]  # the original
    assert len(chain.accepted) == 1
    assert len(sent) == 2 and sent[0] == sent[1]  # nothing was signed again
    assert sender.wait(pending, timeout=10)["status"] == 1
    assert sender.submit(_Fn(), gas=100_000).nonce == 1


def test_receipt_lagging_the_nonce_is_waited_for_before_the_nonce_is_given_up():
    chain = _LaggingSkaled()
    sender, _, _ = _sender(chain)
    sent = _answer_lost_after_mining(chain)
    chain.lag_reads = 2  # the first two lookups of the receipt find nothing
    pending = sender.submit(_Fn(), gas=100_000)
    assert pending.nonce == 0
    assert len(chain.accepted) == 1
    assert len(sent) == 2
    assert sender.wait(pending, timeout=10)["status"] == 1


def test_nonce_used_without_our_receipt_after_a_lost_answer_is_unknowable():
    """The nonce is past ours but no receipt of ours can be found: either
    another writer took it or the receipt is not served. Re-signing on a new
    nonce could run the action twice, so the outcome is left unknown."""
    chain = FakeSkaled()
    sender, account, _ = _sender(chain)
    real = chain.send_raw
    calls = []

    def send_raw(raw):
        calls.append(bytes(raw))
        if len(calls) == 1:
            chain.committed[account.address] = 1  # their nonce-0 tx mined
            raise requests.ConnectionError("reset by peer")
        return real(raw)

    chain.send_raw = send_raw
    with pytest.raises(requests.ConnectionError, match="reset by peer"):
        sender.submit(_Fn(), gas=100_000)
    assert len(calls) == 2 and calls[0] == calls[1]  # the resend, nothing else
    assert chain.accepted == []
    follow = sender.submit(_Fn(), gas=100_000)
    assert follow.nonce == 1  # nonce 0 counted as used
    assert sender.wait(follow, timeout=10)["status"] == 1


def test_fee_refusal_on_the_resend_does_not_move_the_action():
    """skaled checks the gas price before the nonce and before its queue, so a
    resend can be answered "price too low" while the node holds the first
    copy. Refreshing the fee and re-signing would put a second copy on the next
    nonce."""
    chain = FakeSkaled()
    sender, _, _ = _sender(chain)
    real = chain.send_raw
    calls = []

    def send_raw(raw):
        calls.append(bytes(raw))
        if len(calls) == 2:
            raise FakeRpcError("Transaction gas price lower than current eth_gasPrice.")
        try:
            real(raw)
        finally:
            if len(calls) == 1:
                raise requests.ConnectionError("reset by peer")  # answer lost

    chain.send_raw = send_raw
    chain.fee = (200_000, 0)
    with pytest.raises(requests.ConnectionError, match="reset by peer"):
        sender.submit(_Fn(), gas=100_000)
    assert len(calls) == 2 and calls[0] == calls[1]
    assert len(chain.accepted) == 1  # the node holds the first copy only
    follow = sender.submit(_Fn(), gas=100_000)
    assert follow.nonce == 1
    sender.wait(follow, timeout=10)
    assert len(chain.accepted) == 2  # the first copy and the next action
    assert [a["nonce"] for a in chain.accepted] == [0, 1]


def test_mtm_off_resend_keeps_its_nonce_and_waits_for_a_slot():
    """MTM off: the resend is refused because the nonce is above the committed
    one while ours below it are in flight. Same bytes on the same nonce once
    those landed. A resend's answer does not switch to one-at-a-time: only a
    first send's refusal proves MTM is off."""
    chain = FakeSkaled(mtm=False)
    sender, _, _ = _sender(chain, max_in_flight=8)
    first = sender.submit(_Fn(), gas=100_000)
    chain.send_errors.append(requests.ConnectionError("reset by peer"))
    second = sender.submit(_Fn(), gas=100_000)
    assert sender.max_in_flight == 8
    assert second.nonce == first.nonce + 1
    assert len(chain.accepted) == 2
    sender.wait_all([first, second], timeout=10)


def test_stale_nonce_read_after_a_lost_answer_never_moves_the_action():
    """Our copy mined with another of ours, its answer was lost, the resend is
    "invalid nonce" and the nonce read comes from a node a block behind: it
    looks like MTM is off. Only the same bytes may go out again, on the same
    nonce; a resync to the fresh nonce would run the action twice."""
    chain = _StaleSkaled()
    sender, _, _ = _sender(chain, max_in_flight=8, mine_on_sleep=False)
    other = sender.submit(_Fn(), gas=100_000)  # nonce 0, in flight
    sent = _answer_lost_after_mining(chain)  # mines nonce 0 and our nonce 1
    chain.stale_reads.append(0)
    chain.lag_reads = _OWN_RECEIPT_LOOKUPS  # the first lookup round finds nothing
    pending = sender.submit(_Fn(), gas=100_000)
    assert pending.nonce == 1
    assert pending.tx_hash == chain.accepted[1]["hash"]  # the original
    assert len(sent) >= 2 and all(raw == sent[0] for raw in sent)  # never re-signed
    assert [a["nonce"] for a in chain.accepted] == [0, 1]  # the action ran once
    assert sender.max_in_flight == 8
    assert sender.wait(pending, timeout=10)["status"] == 1
    assert sender.wait(other, timeout=10)["status"] == 1
    assert sender.submit(_Fn(), gas=100_000).nonce == 2


@pytest.mark.parametrize(
    "stale, earlier_in_flight",
    [
        (1, True),  # equal to ours: no MTM rule makes ours "invalid"
        (0, False),  # below ours, but nothing of ours is in flight below it
    ],
)
def test_stale_nonce_read_that_mtm_off_cannot_explain_is_unknowable(
    stale, earlier_in_flight
):
    chain = _StaleSkaled()
    sender, _, _ = _sender(chain, max_in_flight=8, mine_on_sleep=False)
    earlier = sender.submit(_Fn(), gas=100_000)  # nonce 0
    if not earlier_in_flight:
        chain.mine()
        sender.wait(earlier, timeout=10)
    sent = _answer_lost_after_mining(chain)  # our nonce-1 copy mines
    chain.stale_reads.append(stale)
    chain.lag_reads = _OWN_RECEIPT_LOOKUPS
    with pytest.raises(requests.ConnectionError, match="reset by peer"):
        sender.submit(_Fn(), gas=100_000)
    assert len(sent) == 2 and sent[0] == sent[1]  # the resend, nothing else
    assert [a["nonce"] for a in chain.accepted] == [0, 1]
    assert sender.max_in_flight == 8
    ours = PendingTx(tx_hash=chain.accepted[1]["hash"], nonce=1)
    assert sender.wait(ours, timeout=10)["status"] == 1  # the first hash is tracked
    assert sender.submit(_Fn(), gas=100_000).nonce == 2  # nonce 1 counted as used


def test_nonce_taken_after_waiting_on_our_lower_nonces_is_unknowable():
    """Only the FIRST resend's "same nonce" answer may move the action on. One
    that follows a wait for our lower nonces does not: on an MTM chain the wait
    was only reachable because a nonce read contradicted the node's answer."""
    chain = FakeSkaled()
    sender, _, _ = _sender(chain, max_in_flight=8)
    sender.submit(_Fn(), gas=100_000)  # nonce 0, in flight
    lost = requests.ConnectionError("reset by peer")
    chain.send_errors.extend(
        [
            lost,
            FakeRpcError("Invalid transaction nonce."),  # reads as MTM off
            FakeRpcError(
                "Pending transaction with same nonce already exists "
                "(skale: we ignore gas price)."
            ),
        ]
    )
    with pytest.raises(requests.ConnectionError) as info:
        sender.submit(_Fn(), gas=100_000)
    assert info.value is lost
    assert [a["nonce"] for a in chain.accepted] == [0]  # never signed on nonce 2
    assert sender.max_in_flight == 8
    assert sender.submit(_Fn(), gas=100_000).nonce == 2  # nonce 1 counted as used


def test_receipt_lookup_failing_after_a_nonce_refusal_counts_the_nonce():
    chain = FakeSkaled()
    sender, _, _ = _sender(chain)
    _answer_lost_after_mining(chain)
    real_receipts = chain.receipts
    failures = []

    def receipts(hashes):
        if not failures:
            failures.append(1)
            raise requests.ConnectionError("still down")
        return real_receipts(hashes)

    chain.receipts = receipts
    with pytest.raises(requests.ConnectionError, match="reset by peer"):
        sender.submit(_Fn(), gas=100_000)
    assert len(chain.accepted) == 1
    assert sender.submit(_Fn(), gas=100_000).nonce == 1  # nonce 0 counted as used


def test_unanswerable_send_is_counted_and_its_gap_is_filled():
    chain = FakeSkaled()
    sender, account, clock = _sender(chain, stall_after=5)
    # The broadcast never reached the node and neither did its resend.
    chain.send_errors.extend(
        [
            requests.ConnectionError("reset by peer"),
            requests.ConnectionError("reset by peer"),
        ]
    )
    with pytest.raises(requests.ConnectionError):
        sender.submit(_Fn(), gas=100_000)
    later = sender.submit(_Fn(), gas=100_000)
    assert later.nonce == 1  # nonce 0 was counted as used
    receipt = sender.wait(later, timeout=60)  # a filler at nonce 0 unblocks it
    assert receipt["status"] == 1
    filler = [a for a in chain.accepted if a["nonce"] == 0]
    assert len(filler) == 1
    assert Web3.to_checksum_address(filler[0]["tx"]["to"]) == account.address


def test_a_run_of_lost_nonces_is_filled_in_one_pass():
    """An outage that loses 16 answers in a row leaves 16 unknowable nonces.
    One heal pass fills them all: one gap per `stall_after` would hold every
    admin transaction (user trades included) for 16 x 15 s."""
    stall_after = 15
    chain = FakeSkaled()
    sender, account, clock = _sender(chain, stall_after=stall_after)
    for _ in range(16):
        chain.send_errors.extend(
            [
                requests.ConnectionError("reset by peer"),
                requests.ConnectionError("reset by peer"),
            ]
        )
        with pytest.raises(requests.ConnectionError):
            sender.submit(_Fn(), gas=100_000)
    sent_at: dict[int, float] = {}
    real = chain.send_raw

    def send_raw(raw):
        real(raw)
        sent_at[chain.accepted[-1]["nonce"]] = clock()

    chain.send_raw = send_raw
    start = clock()
    following = sender.submit(_Fn(), gas=100_000)
    assert following.nonce == 16
    assert sender.wait(following, timeout=600)["status"] == 1
    assert clock() - start <= 2 * stall_after
    fillers = [a for a in chain.accepted if a["nonce"] < 16]
    assert [a["nonce"] for a in fillers] == list(range(16))
    assert all(
        Web3.to_checksum_address(a["tx"]["to"]) == account.address for a in fillers
    )
    assert len({sent_at[n] for n in range(16)}) == 1  # all in one pass


def test_the_run_stops_at_a_nonce_the_node_still_holds():
    """Lost, queued, lost. The queued one is parked behind the gap, where
    `eth_pendingTransactions` does not list it, so its filler is refused (same
    nonce) and the run stops there; the next pass fills the second gap."""
    chain = FakeSkaled()
    sender, _, clock = _sender(chain, stall_after=5, mine_on_sleep=False)
    chain.lose.update({0, 2})
    lost = sender.submit(_Fn(), gas=100_000)
    queued = sender.submit(_Fn(), gas=100_000)
    lost_too = sender.submit(_Fn(), gas=100_000)
    clock.advance(6)
    sender.poll()
    assert [a["nonce"] for a in chain.accepted] == [0, 1, 2, 0]  # one filler
    chain.mine()
    clock.advance(6)
    sender.poll()  # nonce 1 mined with the filler; now nonce 2 is the gap
    assert [a["nonce"] for a in chain.accepted][-1] == 2
    chain.mine()
    clock.advance(1)
    results = sender.wait_all([lost, queued, lost_too], timeout=5)
    assert isinstance(results[0], TxDropped)
    assert results[1]["status"] == 1
    assert isinstance(results[2], TxDropped)


def test_a_young_transaction_ends_the_run():
    """A nonce whose transaction was sent less than `stall_after` ago is not
    yet provably lost, so the run stops below it."""
    chain = FakeSkaled()
    sender, _, clock = _sender(chain, stall_after=5, mine_on_sleep=False)
    chain.lose.update({0, 1})
    sender.submit(_Fn(), gas=100_000)
    clock.advance(4)
    sender.submit(_Fn(), gas=100_000)  # nonce 1: lost too, but only 2 s old
    clock.advance(2)
    sender.poll()
    assert [a["nonce"] for a in chain.accepted] == [0, 1, 0]


def test_dropped_tx_is_filled_and_reported():
    chain = FakeSkaled()
    sender, account, _ = _sender(chain, stall_after=5)
    chain.lose.add(0)  # node says OK, then loses it
    lost = sender.submit(_Fn(), gas=100_000)
    behind = sender.submit(_Fn(), gas=100_000)
    results = sender.wait_all([lost, behind], timeout=60)
    assert isinstance(results[0], TxDropped)
    assert results[1]["status"] == 1


def test_slow_but_known_tx_gets_no_filler():
    chain = FakeSkaled()
    clock = FakeClock()
    sleeps = {"n": 0}

    def sleep(seconds):
        clock.advance(seconds)
        sleeps["n"] += 1
        if sleeps["n"] > 200:  # ~50 s of no blocks, then the chain moves
            chain.mine()

    sender = AdminTxSender(
        chain, Account.create(), CHAIN_ID, clock=clock, sleep=sleep, stall_after=5
    )
    pending = sender.submit(_Fn(), gas=100_000)
    assert sender.wait(pending, timeout=120)["status"] == 1
    assert len(chain.accepted) == 1  # no filler


def test_nonce_taken_by_someone_else_reports_dropped():
    chain = FakeSkaled()
    sender, account, _ = _sender(chain, stall_after=5, mine_on_sleep=False)
    chain.lose.add(0)
    pending = sender.submit(_Fn(), gas=100_000)
    chain.committed[account.address] = 1  # another writer's nonce-0 tx mined
    with pytest.raises(TxDropped):
        sender.wait(pending, timeout=60)


def test_a_receipt_that_lags_the_nonce_is_not_a_drop():
    chain = _LaggingSkaled()
    sender, _, clock = _sender(chain, stall_after=5, mine_on_sleep=False)
    pending = sender.submit(_Fn(), gas=100_000)
    chain.hidden.add(pending.tx_hash)
    chain.mine()  # the nonce moves, the receipt is not readable yet
    clock.advance(6)
    sender.poll()  # first miss: suspected, not declared
    chain.hidden.clear()
    clock.advance(6)
    sender.poll()
    assert sender.wait(pending, timeout=1)["status"] == 1


def test_lost_filler_still_drops_the_original_and_frees_its_slot():
    chain = FakeSkaled()
    sender, _, clock = _sender(
        chain, stall_after=5, max_in_flight=1, mine_on_sleep=False
    )
    chain.lose.add(0)
    original = sender.submit(_Fn(), gas=100_000)  # accepted, then lost
    chain.lose.add(0)  # the first filler is lost too
    clock.advance(6)
    sender.poll()
    assert len(chain.accepted) == 2  # the original and the first filler
    chain.fee = (400_000, 0)
    clock.advance(6)
    sender.poll()
    assert len(chain.accepted) == 3
    assert chain.accepted[-1]["tx"]["maxFeePerGas"] == 400_000  # a fresh fee
    chain.mine()  # the second filler lands
    clock.advance(1)
    sender.poll()
    with pytest.raises(TxDropped):
        sender.wait(original, timeout=1)
    follow = sender.submit(_Fn(), gas=100_000)  # no slot leaked
    assert follow.nonce == 1
    chain.mine()
    assert sender.wait(follow, timeout=10)["status"] == 1


class _FakeProvider:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def make_request(self, method, params):
        self.calls.append(method)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class _FakeW3:
    def __init__(self, pending_response=None):
        self.provider = _FakeProvider(
            pending_response if pending_response is not None else {"result": []}
        )


_HASH = bytes(range(32))


def test_pending_hashes_lists_the_queue_in_one_read():
    """skaled's eth_getTransactionByHash only finds mined transactions; the
    queue is read once per heal pass from eth_pendingTransactions."""
    listed = HexBytes(_HASH).to_0x_hex().upper().replace("0X", "0x")
    other = "0x" + "ab" * 32
    w3 = _FakeW3(pending_response={"result": [{"hash": listed}, {"hash": other}]})
    assert Web3ChainRpc(w3).pending_hashes() == {_HASH, bytes.fromhex("ab" * 32)}
    assert w3.provider.calls == ["eth_pendingTransactions"]


@pytest.mark.parametrize(
    "response",
    [
        {"error": {"code": -32601, "message": "method not found"}},
        {"result": None},
        requests.ConnectionError("down"),
    ],
)
def test_pending_hashes_is_empty_when_the_queue_cannot_be_read(response):
    """The healer copes with a wrong "not queued": a filler on a nonce the node
    holds is refused (same nonce) and the run stops there."""
    assert Web3ChainRpc(_FakeW3(pending_response=response)).pending_hashes() == set()


def _raw_receipt(tx_hash: bytes, block: int = 7) -> dict:
    return {
        "transactionHash": Web3.to_hex(tx_hash),
        "blockNumber": hex(block),
        "status": "0x1",
    }


class _BatchProvider:
    """`make_batch_request` answering with whatever the test scripted, as
    web3 returns it (sorted by id, or a lone error object)."""

    def __init__(self, answer):
        self.answer = answer
        self.batches: list[list] = []

    def make_batch_request(self, requests_):
        self.batches.append(list(requests_))
        return self.answer(requests_) if callable(self.answer) else self.answer


def _receipts_rpc(answer) -> tuple[Web3ChainRpc, _BatchProvider]:
    w3 = _FakeW3()
    w3.provider = _BatchProvider(answer)
    return Web3ChainRpc(w3), w3.provider


_A, _B, _C = b"\xaa" * 32, b"\xbb" * 32, b"\xcc" * 32


def _warnings(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "agentpit.onchain.tx_sender" and r.levelno == logging.WARNING
    ]


def test_receipts_in_order_are_matched_to_their_hashes(caplog):
    rpc, _ = _receipts_rpc(
        [
            {"id": 1, "result": _raw_receipt(_A)},
            {"id": 2, "result": None},
            {"id": 3, "result": _raw_receipt(_C)},
        ]
    )
    got = rpc.receipts([_A, _B, _C])
    assert bytes(got[0]["transactionHash"]) == _A and got[0]["blockNumber"] == 7
    assert got[1] is None
    assert bytes(got[2]["transactionHash"]) == _C
    assert _warnings(caplog) == []


def test_a_short_batch_answer_never_shifts_receipts_onto_other_hashes(caplog):
    """The node answered two of three items: the receipts land on the hashes
    they belong to, never on their neighbours by position."""
    rpc, _ = _receipts_rpc(
        [
            {"id": 2, "result": _raw_receipt(_B)},
            {"id": 3, "result": _raw_receipt(_C)},
        ]
    )
    got = rpc.receipts([_A, _B, _C])
    assert got[0] is None
    assert bytes(got[1]["transactionHash"]) == _B
    assert bytes(got[2]["transactionHash"]) == _C
    assert len(_warnings(caplog)) == 1


def test_an_item_error_or_a_foreign_receipt_reads_as_not_mined(caplog):
    """One bad item must not drop the poll for every other hash."""
    rpc, _ = _receipts_rpc(
        [
            {"id": 1, "error": {"code": -32000, "message": "busy"}},
            {"id": 2, "result": _raw_receipt(b"\xdd" * 32)},  # not one we asked for
            {"id": 3, "result": _raw_receipt(_C)},
        ]
    )
    got = rpc.receipts([_A, _B, _C])
    assert got[0] is None and got[1] is None
    assert bytes(got[2]["transactionHash"]) == _C
    assert len(_warnings(caplog)) == 1  # one line per poll, not per item


def test_receipts_are_asked_for_in_batches_of_100():
    hashes = [i.to_bytes(32, "big") for i in range(1, 251)]
    rpc, provider = _receipts_rpc(
        lambda reqs: [
            {"id": n, "result": _raw_receipt(bytes.fromhex(params[0][2:]))}
            for n, (_, params) in enumerate(reqs)
        ]
    )
    got = rpc.receipts(hashes)
    assert [len(b) for b in provider.batches] == [100, 100, 50]
    assert [bytes(r["transactionHash"]) for r in got] == hashes


def test_a_failed_batch_still_raises():
    rpc, _ = _receipts_rpc({"error": {"code": -32005, "message": "rate limited"}})
    with pytest.raises(RuntimeError, match="receipt batch failed"):
        rpc.receipts([_A])
