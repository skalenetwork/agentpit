"""Adapters for faking on-chain market preparation in sync tests."""

from hexbytes import HexBytes

from agentpit.onchain.ctf_ids import condition_id
from tests.fake_skaled import FakeFn


def as_batch(single):
    """Turn a fake `prepare_market_on_chain(admin, question, labels)` into a
    fake `prepare_markets_on_chain(admin, items)`: one result per item, an
    exception in place of a market that raised."""

    def batch(admin, items):
        out = []
        for question, labels in items:
            try:
                out.append(single(admin, question, labels))
            except Exception as exc:  # noqa: BLE001 - mirrors the real batch
                out.append(exc)
        return out

    return batch


class _MarketCall:
    """A contract call whose calldata names what it does to which condition:
    b"P" + condition id for prepareCondition, b"R" + it for registerToken."""

    address = FakeFn.address

    def __init__(self, data: bytes):
        self.data = data

    def build_transaction(self, tx):
        return {**tx, "to": self.address, "data": "0x" + self.data.hex(), "value": 0}


class SkaledAdmin:
    """Just enough OnchainAdmin for `prepare_markets_on_chain` and the sync,
    with every transaction going through a real `AdminTxSender` to a
    `FakeSkaled`. A condition reads prepared (its tokens registered) once its
    prepareCondition (registerToken) has mined."""

    oracle_address = "0x00000000000000000000000000000000000000a1"
    collateral_address = "0x00000000000000000000000000000000000000c0"

    def __init__(self, chain, sender, *, sync_chunk_size: int = 32):
        self.chain = chain
        self.sender = sender
        self.sync_chunk_size = sync_chunk_size

    def prepare_condition_call(self, oracle, question_id, slots):
        return _MarketCall(b"P" + condition_id(oracle, question_id, slots)), 120_000

    def register_token_call(self, token_a, token_b, condition_id_):
        return _MarketCall(b"R" + bytes(condition_id_)), 220_000

    def submit_many(self, calls):
        return self.sender.submit_many(calls)

    def wait_all(self, pendings, *, timeout):
        return self.sender.wait_all(pendings, timeout=timeout)

    def read_market_states(self, markets):
        ran = {
            bytes(HexBytes(a["tx"]["data"]))
            for a in self.chain.accepted
            if a["hash"] in self.chain.mined
        }
        out = []
        for cid, tokens in markets:
            registered = b"R" + bytes(cid) in ran
            out.append(
                (
                    2 if b"P" + bytes(cid) in ran else 0,
                    tokens[1] if registered else 0,
                    tokens[0] if registered else 0,
                )
            )
        return out
