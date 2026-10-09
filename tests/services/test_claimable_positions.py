"""A resolved market you have won is not a live position.

`list_positions` filters on balance alone, so a winning token still held
appears among OPEN positions priced by `_cur_price`. A resolved market has no
live book — the mirror cancels its orders when the market leaves the active
set — so that falls through to the last trade print. A share worth exactly $1
was being shown at whatever it last changed hands for.
"""

from __future__ import annotations

import json
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


def _insert_trade(
    conn, *, market: str, asset: str, price: int, size: int, taker_api_key: str
) -> None:
    """One CONFIRMED fill with the user as TAKER, priced away from $1 so a
    stale last-trade print is distinguishable from the redeemed $1 payout."""
    conn.execute(
        "INSERT INTO trades (TRADE_ID, MARKET, ASSET_ID, SIDE, PRICE, TRADE_SIZE, "
        "STATUS, TAKER_API_KEY, MAKER_ORDERS, MATCH_TIME) "
        "VALUES (%s, %s, %s, 'BUY', %s, %s, 'CONFIRMED', %s, %s, 0)",
        (
            uuid.uuid4().hex,
            market,
            asset,
            price,
            size,
            taker_api_key,
            json.dumps([{"side": "SELL"}]),
        ),
    )


class _StubOnchain:
    """Fixed CTF balance per token id -- all `list_positions` reads from it."""

    def __init__(self, balances: dict[str, int]):
        self._balances = balances
        self.batch_calls = 0

    def ctf_balance(self, _eth_address: str, token_id: int) -> int:
        return self._balances.get(str(token_id), 0)

    def ctf_balances(self, _eth_address: str, token_ids: list[int]) -> list[int]:
        self.batch_calls += 1
        return [self._balances.get(str(t), 0) for t in token_ids]


def _make_position(
    email: str,
    seed: str,
    *,
    resolved_outcome: int | None,
    held_idx: int,
    balance: int = 100_000_000,
    min_claim_micro: int = 10_000,
):
    """A user who bought 100 shares of one outcome token @ $0.40, in a market
    that is either left ACTIVE or resolved to `resolved_outcome`, and who still
    holds `balance` micro-shares of it on chain.

    Returns the single `PositionWire` `list_positions` produces for it, read by
    a service whose claim minimum is `min_claim_micro`.
    """
    db = fresh_test_db()
    # `list_positions` does `int(token_id)`, so these must parse as ints --
    # unlike the string ids `test_closed_positions.py` uses, which never hits
    # that path.
    base = int.from_bytes(seed.encode(), "big")
    yes_tok, no_tok = str(base * 10 + 1), str(base * 10 + 2)
    with db.write() as conn:
        _uid, acct, api_key = TableWrite.create_user(
            conn,
            email=email,
            password_hash=hash_password("pw12pw12pw12"),
            handle=None,
        )
        req = CreateMarketRequest(
            question="Win?",
            description="d",
            erc1155_tokens=[(yes_tok, "Yes"), (no_tok, "No")],
            slug=f"win-{seed}",
            condition_id=ConditionId(_hex32(seed)),
            state=MarketState.ACTIVE,
        )
        m = TableWrite.create_market(conn, req, is_polygon_market=False)
        held_tok = [yes_tok, no_tok][held_idx]
        _insert_trade(
            conn,
            market=_hex32(seed),
            asset=held_tok,
            price=400_000,
            size=100_000_000,
            taker_api_key=api_key,
        )
        if resolved_outcome is not None:
            TableWrite.resolve_market(
                conn, market_id=m.market_id, winning_outcome_index=resolved_outcome
            )

    onchain = _StubOnchain({held_tok: balance})
    out = AccountService(
        db, onchain=onchain, min_claim_micro=min_claim_micro  # type: ignore[arg-type]
    ).list_positions(acct.address)
    assert len(out) == 1
    return out[0]


@pytest.fixture
def claimable_position():
    """Bought the winning token; the market has since resolved."""
    return _make_position("claimable@x.com", "cp1", resolved_outcome=0, held_idx=0)


@pytest.fixture
def open_position():
    """Bought a token in a market that has not resolved yet."""
    return _make_position("open@x.com", "cp2", resolved_outcome=None, held_idx=0)


@pytest.fixture
def losing_position():
    """Bought the token whose outcome lost."""
    return _make_position("losing@x.com", "cp3", resolved_outcome=0, held_idx=1)


def test_a_claimable_position_is_priced_at_one_dollar(claimable_position):
    p = claimable_position
    assert p.redeemable is True
    assert p.curPrice == 1.0
    assert p.currentValue == p.size


def test_an_open_position_keeps_its_market_price(open_position):
    assert open_position.redeemable is False
    assert open_position.curPrice != 1.0


def test_the_losing_side_of_a_resolved_market_is_not_claimable(losing_position):
    """Holding the outcome that lost is worth nothing and claims nothing."""
    assert losing_position.redeemable is False
    assert losing_position.curPrice == 0.0
    assert losing_position.currentValue == 0.0


@pytest.fixture
def won_dust_position():
    """Won, but holding less than the $0.01 claim minimum."""
    return _make_position(
        "dust@x.com", "cp4", resolved_outcome=0, held_idx=0, balance=9_999
    )


def test_won_dust_is_priced_as_a_win_but_offers_no_claim(won_dust_position):
    """Below the minimum, `PositionService.redeem` answers 400 "nothing to
    claim", so a Claim button here could only fail. The row is still a win:
    settled, priced at $1, and sorted into the closed bucket as won, never as
    a loss."""
    p = won_dust_position
    assert p.settled is True
    assert p.redeemable is False
    assert p.curPrice == 1.0
    assert p.currentValue == pytest.approx(p.size)


def test_a_win_of_exactly_the_minimum_is_claimable():
    p = _make_position(
        "edge@x.com", "cp5", resolved_outcome=0, held_idx=0, balance=10_000
    )
    assert p.redeemable is True
    assert p.curPrice == 1.0


def test_the_minimum_is_the_one_the_service_was_given():
    """The view must use the configured minimum, the one the claim itself
    enforces, and not a constant of its own."""
    p = _make_position(
        "raised@x.com",
        "cp6",
        resolved_outcome=0,
        held_idx=0,
        balance=100_000_000,
        min_claim_micro=100_000_001,
    )
    assert p.redeemable is False
    assert p.curPrice == 1.0
