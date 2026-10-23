"""The position scan asks the chain once, not once per token.

`wallet` looks at two tokens for every market the account has ever
touched. Reading those one at a time is invisible on a local node and brutal
on a remote one: measured against production, an account with 504 trades took
23.6 seconds to render fourteen rows, and the cost tracked the number of
markets touched rather than the number of positions returned.
"""

from __future__ import annotations

import uuid

import pytest

from agentpit.auth.passwords import hash_password
from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.market_state import MarketState
from agentpit.db.table_write import TableWrite
from agentpit.services.account_service import AccountService
from tests.db_helpers import fresh_test_db


def _hex32(seed: str) -> str:
    return "0x" + seed.encode().hex().ljust(64, "0")[:64]


class _CountingOnchain:
    """Answers the batch read and counts it. Calling the single-token reader
    is a test failure: that is the path this work exists to remove."""

    def __init__(self, balances: dict[str, int]):
        self._balances = balances
        self.batch_calls = 0
        self.tokens_asked: list[list[int]] = []

    def ctf_balance(self, _eth_address: str, _token_id: int) -> int:
        raise AssertionError("wallet must not read balances one at a time")

    def balances(self, _eth_address: str, token_ids: list[int]) -> tuple[int, int, list[int]]:
        self.batch_calls += 1
        self.tokens_asked.append(list(token_ids))
        return 7, 42_000_000, [self._balances.get(str(t), 0) for t in token_ids]


def _account_across_markets(count: int, tag: str = "a"):
    """An account that has traded in `count` two-outcome markets.

    Returns the db, the account address, the (yes, no) token ids per market in
    creation order, and the matching condition ids. `tag` keeps two calls in
    one test from colliding on the unique condition id.
    """
    db = fresh_test_db()
    tokens: list[tuple[str, str]] = []
    conditions: list[str] = []
    with db.write() as conn:
        _uid, acct, api_key = TableWrite.create_user(
            conn,
            email=f"scan-{uuid.uuid4().hex[:8]}@x.com",
            password_hash=hash_password("pw12pw12pw12"),
            handle=None,
        )
        for i in range(count):
            seed = f"scan{tag}{i}"
            base = int.from_bytes(seed.encode(), "big")
            yes_tok, no_tok = str(base * 10 + 1), str(base * 10 + 2)
            TableWrite.create_market(
                conn,
                CreateMarketRequest(
                    question=f"Q{i}?",
                    description="d",
                    erc1155_tokens=[(yes_tok, "Yes"), (no_tok, "No")],
                    slug=f"scan-{i}",
                    condition_id=ConditionId(_hex32(seed)),
                    state=MarketState.ACTIVE,
                ),
                is_polygon_market=False,
            )
            conn.execute(
                "INSERT INTO trades (TRADE_ID, MARKET, ASSET_ID, SIDE, PRICE, "
                "TRADE_SIZE, STATUS, TAKER_API_KEY, MAKER_ORDERS, MATCH_TIME) "
                "VALUES (%s,%s,%s,'BUY',400000,100000000,'CONFIRMED',%s,'[]',0)",
                (uuid.uuid4().hex, _hex32(seed), yes_tok, api_key),
            )
            tokens.append((yes_tok, no_tok))
            conditions.append(_hex32(seed))
    return db, acct.address, tokens, conditions


def test_reads_every_balance_in_one_chain_call():
    db, address, tokens, _cids = _account_across_markets(6)
    onchain = _CountingOnchain({tokens[0][0]: 100_000_000})

    wallet = AccountService(db, onchain=onchain).wallet(address)  # type: ignore[arg-type]

    assert onchain.batch_calls == 1, "one call for the whole scan, not one per token"
    # It still had to consider both tokens of all six markets.
    assert len(onchain.tokens_asked[0]) == 12
    assert (wallet.block, wallet.cash_raw, len(wallet.positions)) == (7, 42_000_000, 1)


def test_cost_does_not_grow_with_the_number_of_markets():
    """The regression in one line: twice the markets must not mean twice the
    round trips."""
    small_db, small_addr, small_toks, _ = _account_across_markets(2, tag="s")
    small = _CountingOnchain({small_toks[0][0]: 1_000_000})
    AccountService(small_db, onchain=small).wallet(small_addr)  # type: ignore[arg-type]

    big_db, big_addr, big_toks, _ = _account_across_markets(20, tag="b")
    big = _CountingOnchain({big_toks[0][0]: 1_000_000})
    AccountService(big_db, onchain=big).wallet(big_addr)  # type: ignore[arg-type]

    assert small.batch_calls == big.batch_calls == 1

def test_each_balance_lands_on_its_own_token():
    """A batch read hands back a list; pairing it with the wrong token would
    report a position on a market the account never held."""
    db, address, tokens, _cids = _account_across_markets(4)
    held_yes = tokens[2][0]          # third market's YES
    held_no = tokens[0][1]           # first market's NO
    onchain = _CountingOnchain({held_yes: 7_000_000, held_no: 3_000_000})

    out = AccountService(db, onchain=onchain).wallet(address).positions  # type: ignore[arg-type]

    by_asset = {p.asset: p for p in out}
    assert set(by_asset) == {held_yes, held_no}
    assert by_asset[held_yes].size == pytest.approx(7.0)
    assert by_asset[held_yes].outcome == "Yes"
    assert by_asset[held_no].size == pytest.approx(3.0)
    assert by_asset[held_no].outcome == "No"


def test_a_market_filter_narrows_what_the_chain_is_asked_for():
    """The filter used to be applied after the balance read, so a request for
    one market still paid for every market the account had touched."""
    db, address, tokens, cids = _account_across_markets(5)
    onchain = _CountingOnchain({tokens[1][0]: 5_000_000})

    out = AccountService(db, onchain=onchain).wallet(  # type: ignore[arg-type]
        address, [cids[1]]
    ).positions

    assert onchain.tokens_asked[0] == [int(tokens[1][0]), int(tokens[1][1])]
    assert [p.asset for p in out] == [tokens[1][0]]


def test_an_account_with_no_markets_reads_only_its_cash_and_the_block():
    db = fresh_test_db()
    with db.write() as conn:
        _uid, acct, _key = TableWrite.create_user(
            conn,
            email=f"empty-{uuid.uuid4().hex[:8]}@x.com",
            password_hash=hash_password("pw12pw12pw12"),
            handle=None,
        )
    onchain = _CountingOnchain({})

    wallet = AccountService(db, onchain=onchain).wallet(acct.address)  # type: ignore[arg-type]

    assert (wallet.block, wallet.cash_raw, wallet.positions) == (7, 42_000_000, [])
    assert onchain.tokens_asked == [[]]
