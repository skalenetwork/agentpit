"""What an error out of `send_user_tx` says about its transaction: the receipt
poll runs after the node has taken the broadcast."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from eth_account import Account

from agentpit.onchain.chain_rpc import (
    SendError,
    classify_send_error,
    failed_before_connecting,
)
from agentpit.onchain.user_wallet import send_user_tx
from tests.services.test_gas_sponsor import _never_connected


def test_a_receipt_poll_that_cannot_connect_does_not_read_as_never_sent():
    # The node took the transaction, then the receipt poll could not even
    # connect (the RPC went away mid-flight). It may well mine, so the error
    # must not pass `failed_before_connecting`: the sponsor and `PositionService`
    # read that as "never reached the node" and drop the pending row and the
    # reservation. It is still a transport error, with the poll's error as cause.
    poll_error = _never_connected()
    eth = Mock()
    eth.get_transaction_count.return_value = 0
    eth.send_raw_transaction.return_value = b"\x01" * 32
    eth.wait_for_transaction_receipt.side_effect = poll_error
    call = Mock()
    call.build_transaction.side_effect = lambda fields: {
        **fields,
        "to": "0x" + "11" * 20,
        "data": "0x",
        "value": 0,
    }
    client = SimpleNamespace(
        web3=SimpleNamespace(eth=eth), deployment=SimpleNamespace(chain_id=31337)
    )

    with pytest.raises(Exception) as caught:
        send_user_tx(client, Account.create(), call, gas=60_000, max_fee=1_000)  # type: ignore[arg-type]

    eth.send_raw_transaction.assert_called_once()
    assert not failed_before_connecting(caught.value)
    assert classify_send_error(caught.value) is SendError.TRANSPORT
    assert caught.value.__cause__ is poll_error
