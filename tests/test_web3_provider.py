import pytest
import requests
from web3.providers.rpc.utils import check_if_retry_on_failure

from agentpit.onchain.web3_client import build_http_provider


def test_chain_id_is_cached():
    p = build_http_provider("http://127.0.0.1:8545")
    assert p.cache_allowed_requests is True
    assert "eth_chainId" in p.cacheable_requests
    assert isinstance(p.request_cache_validation_threshold, int)


def test_reads_retry_but_transactions_are_never_resent():
    p = build_http_provider("http://127.0.0.1:8545")
    allow = p.exception_retry_configuration.method_allowlist
    assert "eth_sendRawTransaction" not in allow
    assert "eth_call" in allow
    assert "eth_getTransactionReceipt" in allow
    assert check_if_retry_on_failure("eth_sendRawTransaction", allow) is False


def test_a_batch_of_transactions_is_never_resent():
    """`make_batch_request` has no retry of its own (only `make_request` has):
    a batch whose answer was lost is the sender's to settle, by resending the
    identical bytes once."""
    p = build_http_provider("http://127.0.0.1:8545")
    posts = []

    def post(*args, **kwargs):
        posts.append(args)
        raise requests.ConnectionError("reset by peer")

    p._request_session_manager.make_post_request = post  # noqa: SLF001
    with pytest.raises(requests.ConnectionError):
        p.make_batch_request([("eth_sendRawTransaction", ["0x00"])])
    assert len(posts) == 1
