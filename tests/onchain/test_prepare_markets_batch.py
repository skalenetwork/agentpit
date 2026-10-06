"""prepare_markets_on_chain: one batch read, all sends before any wait, one
result per input."""

import secrets

from eth_utils import keccak

from agentpit.config import Settings
from agentpit.datastructures.condition_id import ConditionId
from agentpit.onchain.admin import OnchainAdmin
from agentpit.onchain.contracts import Contracts
from agentpit.onchain.ctf_ids import binary_market_ids, condition_id
from agentpit.onchain.deployment import Deployment
from agentpit.onchain.tx_sender import PendingTx
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


class _FakeAdmin:
    """Just enough OnchainAdmin for prepare_markets_on_chain, without a chain:
    the `fail_at`-th submit raises, every other one lands at once."""

    oracle_address = "0x00000000000000000000000000000000000000a1"
    collateral_address = "0x00000000000000000000000000000000000000c0"

    def __init__(self, fail_at: int = 0, error: Exception | None = None):
        self.fail_at = fail_at
        self.error = error
        self.submits: list[tuple[str, bytes]] = []
        self.waited: list[PendingTx] = []
        self.prepared: set[bytes] = set()  # condition ids
        self.registered: set[int] = set()  # token ids
        self.state_reads = 0

    def read_market_states(self, markets):
        self.state_reads += 1
        return [
            (
                2 if cid in self.prepared else 0,
                tokens[1] if tokens[0] in self.registered else 0,
                tokens[0] if tokens[1] in self.registered else 0,
            )
            for cid, tokens in markets
        ]

    def _submit(self, kind: str, key: bytes) -> PendingTx:
        self.submits.append((kind, key))
        if len(self.submits) == self.fail_at:
            raise self.error
        return PendingTx(tx_hash=secrets.token_bytes(32), nonce=len(self.submits))

    def submit_prepare_condition(self, oracle, question_id, slots):
        pending = self._submit("prepare", question_id)
        self.prepared.add(condition_id(oracle, question_id, slots))
        return pending

    def submit_register_token(self, token_a, token_b, condition_id_):
        pending = self._submit("register", condition_id_)
        self.registered.update({token_a, token_b})
        return pending

    def wait_all(self, pendings, *, timeout):
        self.waited.extend(pendings)
        return [{"status": 1} for _ in pendings]


def test_a_failed_submit_stops_the_sends_for_the_rest_of_the_batch():
    """A submit error is never about one market (static gas, no estimate):
    the node is unreachable or our nonce stream is in trouble. Sending the
    rest anyway could leave a nonce gap per remaining market."""
    boom = ConnectionError("read timed out")
    admin = _FakeAdmin(fail_at=3, error=boom)  # plan 2's prepareCondition
    items = [(_q(f"stop {i}"), ["Yes", "No"]) for i in range(5)]
    results = prepare_markets_on_chain(admin, items)

    first_qid = keccak(text=items[0][0])
    first_cid, _ = binary_market_ids(
        admin.oracle_address, admin.collateral_address, first_qid
    )
    assert [kind for kind, _ in admin.submits] == ["prepare", "register", "prepare"]
    assert admin.submits[2][1] == keccak(text=items[1][0])
    assert len(admin.waited) == 2  # plan 1's transactions are still awaited
    assert admin.state_reads == 2  # and still go through the verdict read
    assert not isinstance(results[0], Exception)
    assert results[0][0] == ConditionId("0x" + first_cid.hex())
    assert all(r is boom for r in results[1:])


def test_a_ready_market_after_a_failed_submit_still_passes():
    """Only markets that still need a transaction inherit the submit error."""
    ready_q = _q("ready")
    admin = _FakeAdmin(fail_at=1, error=ConnectionError("down"))
    ready_qid = keccak(text=ready_q)
    ready_cid, ready_tokens = binary_market_ids(
        admin.oracle_address, admin.collateral_address, ready_qid
    )
    admin.prepared.add(ready_cid)
    admin.registered.update(ready_tokens)
    results = prepare_markets_on_chain(
        admin, [(_q("fails"), ["Yes", "No"]), (ready_q, ["Yes", "No"])]
    )
    assert isinstance(results[0], ConnectionError)
    assert not isinstance(results[1], Exception)
    assert len(admin.submits) == 1
