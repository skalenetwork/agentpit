"""prepare_markets_on_chain: one batch read, all sends before any wait, one
result per input."""

import secrets

from agentpit.config import Settings
from agentpit.onchain.admin import OnchainAdmin
from agentpit.onchain.contracts import Contracts
from agentpit.onchain.deployment import Deployment
from agentpit.onchain.web3_client import Web3Client
from agentpit.services.market_service import (
    MarketStateError,
    prepare_market_on_chain,
    prepare_markets_on_chain,
)


def _admin() -> OnchainAdmin:
    settings = Settings()
    d = Deployment.load(settings.deployment_path)
    client = Web3Client(settings, d)
    return OnchainAdmin(client, Contracts(client.web3, d))


def _q(tag: str) -> str:
    return f"Batch prepare {tag} {secrets.token_hex(6)}?"


def _registered(admin, tokens) -> bool:
    reg = admin._contracts.exchange.functions.registry  # noqa: SLF001
    a, _ = reg(int(tokens[0][0])).call()
    b, _ = reg(int(tokens[1][0])).call()
    return a == int(tokens[1][0]) and b == int(tokens[0][0])


def test_batch_prepares_and_registers_every_new_market():
    admin = _admin()
    q1, q2, q3 = _q("a"), _q("b"), _q("c")
    items = [(q1, ["Yes", "No"]), (q2, ["Yes", "No"]), (q3, ["Up", "Down"]), (q1, ["Yes", "No"])]
    results = prepare_markets_on_chain(admin, items)
    assert len(results) == 4
    assert not any(isinstance(r, Exception) for r in results)
    assert results[0][0] == results[3][0]  # same question, same condition
    assert len({r[0] for r in results}) == 3
    assert results[2][1][0][1] == "Up" and results[2][1][1][1] == "Down"
    for _, tokens in results:
        assert _registered(admin, tokens)


def test_already_prepared_condition_only_gets_registered():
    admin = _admin()
    q = _q("prepared")
    from eth_utils import keccak

    admin.prepare_condition(admin.oracle_address, keccak(text=q), 2)
    (result,) = prepare_markets_on_chain(admin, [(q, ["Yes", "No"])])
    assert not isinstance(result, Exception)
    assert _registered(admin, result[1])


def test_fully_prepared_market_sends_nothing():
    admin = _admin()
    q = _q("done")
    first = prepare_market_on_chain(admin, q, ["Yes", "No"])
    w3 = admin._client.web3  # noqa: SLF001
    before = w3.eth.get_transaction_count(admin.oracle_address)
    (again,) = prepare_markets_on_chain(admin, [(q, ["Yes", "No"])])
    assert again == first
    assert w3.eth.get_transaction_count(admin.oracle_address) == before


def test_non_binary_market_fails_alone():
    admin = _admin()
    good = _q("good")
    results = prepare_markets_on_chain(
        admin, [(_q("three"), ["A", "B", "C"]), (good, ["Yes", "No"])]
    )
    assert isinstance(results[0], MarketStateError)
    assert not isinstance(results[1], Exception)


def test_single_wrapper_raises_the_items_error():
    admin = _admin()
    try:
        prepare_market_on_chain(admin, _q("single"), ["A", "B", "C"])
    except MarketStateError:
        pass
    else:
        raise AssertionError("expected MarketStateError")
