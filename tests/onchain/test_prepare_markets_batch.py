"""prepare_markets_on_chain: one batch read, all sends before any wait, one
result per input."""

import secrets

from eth_utils import keccak

from agentpit.config import Settings
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


def test_twenty_new_markets_take_one_submit_many_on_chain(monkeypatch):
    admin = _admin()
    sizes = []
    real = admin.submit_many

    def spy(calls):
        sizes.append(len(calls))
        return real(calls)

    monkeypatch.setattr(admin, "submit_many", spy)
    items = [(_q(f"twenty {i}"), ["Yes", "No"]) for i in range(20)]
    results = prepare_markets_on_chain(admin, items)
    assert sizes == [40]
    assert not any(isinstance(r, Exception) for r in results)
    for _, tokens in results:
        assert _registered(admin, tokens)


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
    `submit_many` lands every call at once, except the ones `item_errors`
    names by their place in the batch; with `error` it raises as a whole."""

    oracle_address = "0x00000000000000000000000000000000000000a1"
    collateral_address = "0x00000000000000000000000000000000000000c0"

    def __init__(
        self,
        item_errors: dict[int, Exception] | None = None,
        error: Exception | None = None,
    ):
        self.item_errors = item_errors or {}
        self.error = error
        self.batches: list[list[tuple]] = []
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

    def prepare_condition_call(self, oracle, question_id, slots):
        return ("prepare", oracle, question_id, slots), 120_000

    def register_token_call(self, token_a, token_b, condition_id_):
        return ("register", token_a, token_b, condition_id_), 220_000

    def submit_many(self, calls):
        self.batches.append([call for call, _ in calls])
        if self.error is not None:
            raise self.error
        out: list[PendingTx | Exception] = []
        for i, (call, _gas) in enumerate(calls):
            if i in self.item_errors:
                out.append(self.item_errors[i])
                continue
            if call[0] == "prepare":
                self.prepared.add(condition_id(*call[1:]))
            else:
                self.registered.update(call[1:3])
            out.append(PendingTx(tx_hash=secrets.token_bytes(32), nonce=i))
        return out

    def wait_all(self, pendings, *, timeout):
        self.waited.extend(pendings)
        return [{"status": 1} for _ in pendings]


def test_a_chunk_goes_out_as_one_submit_many():
    admin = _FakeAdmin()
    items = [(_q(f"chunk {i}"), ["Yes", "No"]) for i in range(20)]
    results = prepare_markets_on_chain(admin, items)
    assert len(admin.batches) == 1
    # A market's prepareCondition and registerToken go into the same batch.
    assert [call[0] for call in admin.batches[0]] == ["prepare", "register"] * 20
    assert len(admin.waited) == 40
    assert not any(isinstance(r, Exception) for r in results)


def test_a_failed_prepare_item_fails_only_its_market():
    boom = RuntimeError("insufficient funds for gas * price + value")
    admin = _FakeAdmin(item_errors={2: boom})  # market 1's prepareCondition
    items = [(_q(f"item {i}"), ["Yes", "No"]) for i in range(3)]
    results = prepare_markets_on_chain(admin, items)
    assert results[1] is boom
    assert not isinstance(results[0], Exception)
    assert not isinstance(results[2], Exception)
    assert len(admin.waited) == 5  # every call that went out is still awaited


def test_a_failed_register_item_is_left_to_the_verdict_read():
    admin = _FakeAdmin(item_errors={1: RuntimeError("answer lost")})
    (result,) = prepare_markets_on_chain(admin, [(_q("register"), ["Yes", "No"])])
    assert isinstance(result, MarketStateError)  # the chain says: not registered
    assert admin.state_reads == 2


def test_submit_many_raising_fails_every_market_that_needed_a_transaction():
    """A whole-batch error is never about one market: every market still
    needing a transaction fails with it and the next sync pass retries."""
    boom = ConnectionError("read timed out")
    admin = _FakeAdmin(error=boom)
    ready_q = _q("ready")
    ready_cid, ready_tokens = binary_market_ids(
        admin.oracle_address, admin.collateral_address, keccak(text=ready_q)
    )
    admin.prepared.add(ready_cid)
    admin.registered.update(ready_tokens)
    results = prepare_markets_on_chain(
        admin,
        [(_q("fails"), ["Yes", "No"]), (ready_q, ["Yes", "No"]), (_q("too"), ["Yes", "No"])],
    )
    assert results[0] is boom and results[2] is boom
    assert not isinstance(results[1], Exception)
    assert len(admin.batches) == 1
    assert admin.waited == []
