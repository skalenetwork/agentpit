"""Gamma rows and fakes of on-chain market preparation for sync tests."""

import secrets

from hexbytes import HexBytes

from agentpit.onchain.ctf_ids import condition_id
from tests.fake_skaled import FakeFn


def gamma_row(**over) -> dict:
    tag = secrets.token_hex(4)
    row = {
        "id": str(int(tag, 16)),
        "conditionId": "0x" + secrets.token_hex(32),
        "question": f"Will it happen {tag}?",
        "description": "Rules.",
        "slug": f"will-it-happen-{tag}",
        "startDate": "2026-01-01T00:00:00Z",
        "endDate": "2099-01-01T00:00:00Z",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": f'["{int(secrets.token_hex(8), 16)}", "{int(secrets.token_hex(8), 16)}"]',
        "version": "v1",
        "closed": False,
        "acceptingOrders": True,
        "volume24hr": 50_000,
        "tags": [],
        "events": [{"id": f"ev-{tag}", "slug": f"event-{tag}", "title": f"Event {tag}"}],
    }
    row.update(over)
    return row


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

    def report_payouts_call(self, question_id, payouts):
        return (
            _MarketCall(b"Q" + condition_id(self.oracle_address, question_id, 2)),
            165_000,
        )

    def submit_many(self, calls):
        return self.sender.submit_many(calls)

    def wait_all(self, pendings, *, timeout):
        return self.sender.wait_all(pendings, timeout=timeout)

    def _ran(self) -> set[bytes]:
        return {
            bytes(HexBytes(a["tx"]["data"]))
            for a in self.chain.accepted
            if a["hash"] in self.chain.mined
        }

    def payout_denominators(self, condition_ids):
        ran = self._ran()
        return [int(b"Q" + bytes(cid) in ran) for cid in condition_ids]

    def read_market_states(self, markets):
        ran = self._ran()
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
