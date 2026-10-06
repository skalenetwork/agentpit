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
