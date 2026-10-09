"""What an error out of `send_user_tx` says about its transaction, against a
fake web3: the receipt poll runs after the node has taken the broadcast."""

from types import SimpleNamespace

import pytest
import requests
from eth_account import Account
from urllib3.exceptions import MaxRetryError, NewConnectionError

from agentpit.onchain.chain_rpc import (
    SendError,
    classify_send_error,
    failed_before_connecting,
)
from agentpit.onchain.user_wallet import send_user_tx


class _Eth:
    """The three calls `send_user_tx` makes with a limit and a fee given. The
    node takes every broadcast; the receipt poll raises `poll_error`."""

    def __init__(self, poll_error: Exception):
        self.poll_error = poll_error
        self.broadcast: list[bytes] = []

    def get_transaction_count(self, _address, _block):
        return 0

    def send_raw_transaction(self, raw: bytes) -> bytes:
        self.broadcast.append(raw)
        return b"\x01" * 32

    def wait_for_transaction_receipt(self, _tx_hash, timeout):
        raise self.poll_error


class _Call:
    def build_transaction(self, fields: dict) -> dict:
        return {**fields, "to": "0x" + "11" * 20, "data": "0x", "value": 0}


def test_a_receipt_poll_that_cannot_connect_does_not_read_as_never_sent():
    """The node took the transaction, then the receipt poll could not even
    connect (the RPC went away mid-flight). The transaction may well mine, so
    the error must not pass `failed_before_connecting`: the sponsor and
    `PositionService` read that as "the broadcast never reached the node" and
    drop the pending row and the reservation. It is still a transport error,
    an outcome nobody knows, with the poll's own error as its cause."""
    poll_error = requests.ConnectionError(
        MaxRetryError(
            None, "/", NewConnectionError(None, "Failed to establish a new connection")
        )
    )
    eth = _Eth(poll_error)
    client = SimpleNamespace(
        web3=SimpleNamespace(eth=eth), deployment=SimpleNamespace(chain_id=31337)
    )

    with pytest.raises(Exception) as caught:
        send_user_tx(client, Account.create(), _Call(), gas=60_000, max_fee=1_000)  # type: ignore[arg-type]

    assert len(eth.broadcast) == 1
    assert not failed_before_connecting(caught.value)
    assert classify_send_error(caught.value) is SendError.TRANSPORT
    assert caught.value.__cause__ is poll_error
