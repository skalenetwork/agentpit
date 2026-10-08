"""The node calls `AdminTxSender` makes, and how it reads the answers.

`ChainRpc` is what the sender needs from a node; `Web3ChainRpc` provides it
over a web3 HTTP provider (a fake skaled does in tests). This layer keeps no
sender state: it sends, reads and classifies. Whether a failed send may still
be held by the node (`classify_send_error`, `failed_before_connecting`), and
how one JSON-RPC batch's answers are placed on its requests, are decided here;
what to do about it is `tx_sender`'s business.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Protocol

import requests
from eth_utils import keccak
from hexbytes import HexBytes
from urllib3.exceptions import ConnectTimeoutError, MaxRetryError
from web3 import Web3
from web3._utils.method_formatters import receipt_formatter
from web3._utils.validation import KNOWN_REQUEST_TIMEOUT_MESSAGING
from web3.datastructures import AttributeDict
from web3.exceptions import RequestTimedOut, Web3RPCError
from web3.types import RPCEndpoint, TxReceipt

log = logging.getLogger(__name__)

# SKALE refuses a JSON-RPC batch above 128 requests.
_RPC_BATCH = 100


class SendError(Enum):
    DUPLICATE = "duplicate"  # the node already holds this very transaction
    NONCE_TAKEN = "nonce_taken"  # another transaction holds the nonce
    NONCE_INVALID = "nonce_invalid"  # below committed, or above it with MTM off
    FEE_LOW = "fee_low"
    QUEUE_FULL = "queue_full"  # the node's transaction queue is full
    BALANCE_LOW = "balance_low"  # the account cannot pay for the transaction
    TRANSPORT = "transport"  # no answer: the node may or may not hold it
    OTHER = "other"  # any other answer: refused


# Lowercase substrings of skaled's answers, plus geth/anvil wording. Checked in
# this order, so "replacement transaction underpriced" is NONCE_TAKEN, not
# FEE_LOW.
_ANSWERS = (
    (
        SendError.DUPLICATE,
        (
            "same transaction already exists",
            "already in the blockchain",
            "already known",
            "known transaction",
            "transaction already imported",
        ),
    ),
    (
        SendError.NONCE_TAKEN,
        ("same nonce already exists", "replacement transaction underpriced"),
    ),
    (SendError.NONCE_INVALID, ("invalid transaction nonce", "nonce too low")),
    (
        SendError.FEE_LOW,
        (
            "lower than current eth_gasprice",
            "less than block base fee",
            "transaction underpriced",
        ),
    ),
    # skaled checks both before it imports the transaction
    # (libweb3jsonrpc/Eth.cpp, exceptionToErrorMessage).
    (SendError.QUEUE_FULL, ("transaction queue is full",)),
    (SendError.BALANCE_LOW, ("account balance is too low",)),
)


def classify_send_error(exc: BaseException) -> SendError:
    """What a failed `eth_sendRawTransaction` says about the transaction."""
    if isinstance(
        exc,
        (
            requests.ConnectionError,
            requests.Timeout,
            requests.exceptions.ChunkedEncodingError,  # the body was cut
            ConnectionError,
            TimeoutError,
            RequestTimedOut,
        ),
    ):
        return SendError.TRANSPORT
    if isinstance(exc, requests.HTTPError):
        # A proxy's 502/503/504 says nothing about the node: the request may
        # have reached it. Any other status (429, 4xx) means it was not taken.
        # `is not None`: a Response is falsy for exactly these statuses.
        response = exc.response
        if response is not None and response.status_code >= 500:
            return SendError.TRANSPORT
    text = str(exc).lower()
    for kind, markers in _ANSWERS:
        if any(marker in text for marker in markers):
            return kind
    return SendError.OTHER


def failed_before_connecting(exc: BaseException) -> bool:
    """Did this failed request provably never reach the node?

    Only a failure to open the connection says so: a refused or timed-out TCP
    connect, or a host name that did not resolve. requests raises those as
    `ConnectTimeout`, or as a `ConnectionError` wrapping urllib3's
    `MaxRetryError` whose reason is a `NewConnectionError` (refused, DNS) or a
    `ConnectTimeoutError`. A read timeout, or a reset or a hang-up after
    connecting, may come after the node took the request: not this.

    Only the exception requests wrapped (its first argument, or an explicit
    `raise ... from`) is looked at, never `__context__` or anything deeper: a
    reset raised while an earlier refusal was being handled carries that
    refusal in its context, yet its own request did connect. A wrong True
    would report a send the node may hold as refused, so every doubt is
    False.
    """
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return True
    if not isinstance(exc, requests.exceptions.ConnectionError):
        return False
    wrapped = next((a for a in exc.args if isinstance(a, BaseException)), None)
    if wrapped is None:
        wrapped = exc.__cause__
    if isinstance(wrapped, MaxRetryError):
        wrapped = wrapped.reason
    # NewConnectionError (and its NameResolutionError) is a ConnectTimeoutError.
    return isinstance(wrapped, ConnectTimeoutError)


class BatchUnanswered(ConnectionError):
    """A batch of sends whose answer cannot be placed item by item: cut
    short, unreadable, or not pairing one to one with the requests. The node
    may hold any of them, so it counts as no answer at all (TRANSPORT): the
    identical batch goes out once more."""


class BatchRefused(Exception):
    """The node answered a whole batch with one error object, without looking
    at any item: skaled does this to a batch above 128 requests."""


class ChainRpc(Protocol):
    """The node calls the sender makes. `Web3ChainRpc` in production, a fake
    skaled in tests."""

    def nonce(self, address: str, block: str) -> int: ...

    def send_raw(self, raw: bytes) -> None: ...

    def send_raw_batch(self, raws: list[bytes]) -> list[Exception | None]: ...

    def pending_hashes(self) -> set[bytes]: ...

    def receipts(self, tx_hashes: list[bytes]) -> list[TxReceipt | None]: ...

    def estimate_gas(self, tx: dict) -> int: ...

    def fee_params(self) -> tuple[int, int]: ...

    def balance(self, address: str) -> int: ...


class Web3ChainRpc:
    """`ChainRpc` over a web3 HTTP provider."""

    def __init__(self, web3: Web3):
        self._w3 = web3

    def nonce(self, address: str, block: str) -> int:
        return self._w3.eth.get_transaction_count(address, block)  # type: ignore[arg-type]

    def send_raw(self, raw: bytes) -> None:
        self._w3.eth.send_raw_transaction(raw)

    def send_raw_batch(self, raws: list[bytes]) -> list[Exception | None]:
        """Broadcast many signed transactions in one JSON-RPC batch. One answer
        per transaction, in order: None if the node took it, else the error
        web3 would have raised for a single send of it.

        Through the provider: web3's `batch_requests()` refuses
        eth_sendRawTransaction, the provider call does not, and the provider
        never retries a batch. Raises for the whole batch when no answer can be
        placed on its own transaction: the transport's exception as it stands
        (so a failed connect still reads as one), `BatchUnanswered` for an
        answer that cannot be read or paired one to one, `BatchRefused` for
        one error object in place of the list.
        """
        hashes = [keccak(raw) for raw in raws]
        try:
            responses = self._w3.provider.make_batch_request(
                [  # type: ignore[misc]
                    (RPCEndpoint("eth_sendRawTransaction"), [Web3.to_hex(raw)])
                    for raw in raws
                ]
            )
        except Exception as exc:
            if (
                isinstance(exc, requests.RequestException)
                or classify_send_error(exc) is SendError.TRANSPORT
            ):
                raise
            # Raised after the post: the body was not JSON, or its answers
            # could not be sorted by id. The node may have taken any of them.
            raise BatchUnanswered(f"batch answer unreadable: {exc!r}") from exc
        if isinstance(responses, list):
            return _match_send_answers(hashes, responses)
        if isinstance(responses, dict) and responses.get("error") is not None:
            raise BatchRefused(f"node refused the whole batch: {responses['error']!r}")
        raise BatchUnanswered(f"batch answer is not a list: {responses!r:.200}")

    def pending_hashes(self) -> set[bytes]:
        """Hashes in the node's queue, in one read of `eth_pendingTransactions`.

        skaled's `eth_getTransactionByHash` finds only mined transactions, so
        this list is the only way to see a queued one. It is the CURRENT
        queue only (`Client::pending` is `topTransactions(status().current)`):
        a transaction parked behind a nonce gap is not in it. If the list
        cannot be read the answer is empty; the stall healer copes with a
        wrong "not queued" (a filler on a nonce the node holds is refused).
        """
        try:
            response = self._w3.provider.make_request(
                RPCEndpoint("eth_pendingTransactions"), []
            )
            queued = response.get("result")
            if not isinstance(queued, list):
                return set()
            return {
                bytes(HexBytes(tx["hash"]))
                for tx in queued
                if isinstance(tx, dict) and tx.get("hash")
            }
        except Exception:
            return set()

    def receipts(self, tx_hashes: list[bytes]) -> list[TxReceipt | None]:
        """Receipts for many hashes in one round trip per 100; None = not mined.

        Each receipt is matched to the hash it names in `transactionHash`,
        never by its position: a short answer or a failed item must not shift
        receipts onto other hashes. A hash whose item is missing, failed or
        names another hash reads as not mined for this poll, with one warning
        per call, so one bad item never drops the poll for the others.
        """
        out: list[TxReceipt | None] = []
        problems: list[str] = []
        for start in range(0, len(tx_hashes), _RPC_BATCH):
            chunk = [bytes(h) for h in tx_hashes[start : start + _RPC_BATCH]]
            responses = self._w3.provider.make_batch_request(
                [("eth_getTransactionReceipt", [Web3.to_hex(h)]) for h in chunk]  # type: ignore[misc]
            )
            if not isinstance(responses, list):
                raise RuntimeError(f"receipt batch failed: {responses}")
            out.extend(_match_receipts(chunk, responses, problems))
        if problems:
            log.warning(
                "admin receipt batch answered oddly, those hashes read as not "
                "mined this poll: %s",
                "; ".join(problems[:5]) + ("; ..." if len(problems) > 5 else ""),
            )
        return out

    def estimate_gas(self, tx: dict) -> int:
        return self._w3.eth.estimate_gas(tx)  # type: ignore[arg-type]

    def fee_params(self) -> tuple[int, int]:
        return current_fee_params(self._w3)

    def balance(self, address: str) -> int:
        return self._w3.eth.get_balance(Web3.to_checksum_address(address))


def current_fee_params(web3: Web3) -> tuple[int, int]:
    """(maxFeePerGas, maxPriorityFeePerGas): the node's current `eth_gasPrice`
    and no tip.

    skaled bills `maxFeePerGas` in full (`effectiveGasPrice = maxFeePerGas`,
    not base fee + tip, measured 2026-10-06 on mainnet and testnet), so every
    wei above the current price is paid: web3's default of twice the base fee
    plus a tip doubled every cost. Below the current price the node refuses
    the transaction, and it drops a queued one once the price rises past it.
    anvil and geth bill base fee + tip, and their `eth_gasPrice` is at least
    the base fee.
    """
    return web3.eth.gas_price, 0


def _match_receipts(
    hashes: list[bytes], responses: list, problems: list[str]
) -> list[TxReceipt | None]:
    """One receipt or None per hash in `hashes`, from one batch's answers,
    each placed by the hash it names. What does not fit goes to `problems`."""
    if len(responses) != len(hashes):
        problems.append(f"{len(responses)} answers for {len(hashes)} hashes")
    wanted = set(hashes)
    found: dict[bytes, TxReceipt] = {}
    for response in responses:
        if not isinstance(response, dict) or response.get("error"):
            error = response.get("error") if isinstance(response, dict) else response
            problems.append(f"item failed: {error}")
            continue
        raw = response.get("result")
        if raw is None:
            continue  # not mined yet; nothing says which hash, nothing to place
        try:
            tx_hash = bytes(HexBytes(raw["transactionHash"]))
        except (KeyError, TypeError, ValueError):
            problems.append("a receipt without a transactionHash")
            continue
        if tx_hash not in wanted:
            problems.append(f"a receipt for {HexBytes(tx_hash).to_0x_hex()}, not asked")
            continue
        found[tx_hash] = AttributeDict.recursive(receipt_formatter(raw))
    return [found.get(h) for h in hashes]


def _match_send_answers(hashes: list[bytes], responses: list) -> list[Exception | None]:
    """One answer per transaction from one batch's answers, in order.

    web3 sorts the answers by id and handed the ids out in request order, so
    with exactly one answer per request, position i answers transaction i. A
    refusal does not name its transaction (a receipt does), so nothing short
    of that one-to-one fit can be placed: a missing, extra, repeated or
    non-int id makes the whole answer unusable (web3 hands out int ids, and
    string ids would sort as text, "10" before "9"). An accepted item names
    its hash, and that is checked too.
    """
    if len(responses) != len(hashes):
        raise BatchUnanswered(f"{len(responses)} answers for {len(hashes)} sends")
    ids = [r.get("id") if isinstance(r, dict) else None for r in responses]
    # `type is int`: a JSON `true` would pass isinstance(..., int).
    if not all(type(i) is int for i in ids) or len(set(ids)) != len(ids):
        raise BatchUnanswered(f"batch answers without distinct int ids: {ids!r:.200}")
    out: list[Exception | None] = []
    for tx_hash, response in zip(hashes, responses):
        error = response.get("error")
        if error is not None:
            out.append(_send_error(error, response))
            continue
        result = response.get("result")
        try:
            named = bytes(HexBytes(result)) if isinstance(result, str) else None
        except ValueError:
            named = None
        if named != tx_hash:
            raise BatchUnanswered(
                f"answer {result!r:.80} does not name the transaction at its place"
            )
        out.append(None)
    return out


def _send_error(error, response: dict) -> Exception:
    """What web3 raises for this answer to a single send: `RequestTimedOut`
    for a timeout message (TRANSPORT: the node may hold the transaction),
    otherwise a `Web3RPCError` carrying the node's message."""
    message = error.get("message") if isinstance(error, dict) else error
    if isinstance(message, str) and any(
        marker in message.lower() for marker in KNOWN_REQUEST_TIMEOUT_MESSAGING
    ):
        return RequestTimedOut(repr(error), rpc_response=response)  # type: ignore[arg-type]
    return Web3RPCError(repr(error), rpc_response=response)  # type: ignore[arg-type]
