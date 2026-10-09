"""End-to-end auto-redeem on the local CTF: sync a market, onboard and split,
pay out a YES win, run the pass. No wallet is ever granted gas: the
approvals, splits and claims all go through `UserGasSponsor`, as in production.
"""

from functools import partial

from eth_account import Account

from agentpit.config import Settings
from agentpit.datastructures.market_state import MarketState
from agentpit.db.table_read import TableRead
from agentpit.onchain.tx_sender import TRANSFER_GAS
from agentpit.services.market_service import redeem_resolved_markets
from tests.onchain import _helpers as h

admin, db = h.admin, h.db  # the shared fixtures
# Opted in explicitly: the tests do not depend on the AUTO_REDEEM_ENABLED column default.
_opted_in = partial(h.onboarded_account, auto_redeem=True)


def _market_row(db, market_id: int):
    with db.read() as conn:
        row = TableRead.read_market(conn, market_id)
    assert row is not None
    return row


def test_auto_redeem_pays_winner_and_flags_market(admin, db):
    market = h.synced_market(db, admin)
    tokens = [int(t) for t, _label in market.erc1155_tokens]
    user = _opted_in(db, admin)
    h.split(db, admin, user, market, 100_000_000)  # 100 apUSD raw
    bal_before = admin.usd_balance(user.eth_address)
    assert admin.ctf_balances(user.eth_address, tokens) == [100_000_000, 100_000_000]
    h.resolve_yes(db, admin, market)

    assert redeem_resolved_markets(db, admin, Settings()) == 1

    assert admin.usd_balance(user.eth_address) - bal_before == 100_000_000  # winner paid, loser 0
    assert admin.ctf_balances(user.eth_address, tokens) == [0, 0]
    row = _market_row(db, market.market_id)
    assert row.market_state == MarketState.RESOLVED
    assert row.fully_redeemed is True
    # Idempotent: a second pass redeems nobody.
    assert redeem_resolved_markets(db, admin, Settings()) == 0


def test_auto_redeem_claims_for_a_holder_with_no_native_balance(admin, db):
    """Gasless claims: a winner holding 0 native is paid all the same, the gas booked."""
    settings = Settings()
    market = h.synced_market(db, admin)
    yes_token = int(market.erc1155_tokens[0][0])
    user = _opted_in(db, admin)
    h.split(db, admin, user, market, 50_000_000)
    h.resolve_yes(db, admin, market)
    h.drain_native_balance(admin, user.eth_address)
    assert admin.native_balance(user.eth_address) == 0
    usd_before = admin.usd_balance(user.eth_address)
    booked_before = h.sponsored_gas(db, user.api_key)

    assert redeem_resolved_markets(db, admin, settings) == 1

    assert admin.usd_balance(user.eth_address) - usd_before == 50_000_000
    assert admin.ctf_balance(user.eth_address, yes_token) == 0
    assert _market_row(db, market.market_id).fully_redeemed is True
    # The top-up's own transfer plus the claim's receipt gas.
    assert h.sponsored_gas(db, user.api_key) - booked_before > TRANSFER_GAS
    # Whatever is left is part of one claim's need, far under the ceiling.
    assert admin.native_balance(user.eth_address) <= settings.max_topup_gas * admin.gas_price()


def test_a_holder_who_has_not_opted_in_keeps_their_tokens(admin, db):
    """An opted-out holder is skipped: no transaction, tokens and balance untouched."""
    market = h.synced_market(db, admin)
    tokens = [int(t) for t, _label in market.erc1155_tokens]
    user = h.onboarded_account(db, admin, auto_redeem=False)
    h.split(db, admin, user, market, 100_000_000)
    bal_before = admin.usd_balance(user.eth_address)
    assert admin.ctf_balances(user.eth_address, tokens) == [100_000_000, 100_000_000]
    h.resolve_yes(db, admin, market)
    nonce_before = admin.transaction_count(user.eth_address)

    assert redeem_resolved_markets(db, admin, Settings()) == 0

    assert admin.transaction_count(user.eth_address) == nonce_before
    assert admin.usd_balance(user.eth_address) == bal_before
    assert admin.ctf_balances(user.eth_address, tokens) == [100_000_000, 100_000_000]
    row = _market_row(db, market.market_id)
    assert row.market_state == MarketState.RESOLVED
    assert row.fully_redeemed is False


def test_auto_redeem_stops_at_the_per_pass_cap(admin, db):
    """Cap of one claim per pass: the newest market is paid, the older one waits for the next pass."""
    capped = Settings().model_copy(update={"auto_redeem_max_per_pass": 1})
    market_b, market_a = h.synced_market(db, admin), h.synced_market(db, admin)
    yes_b = int(market_b.erc1155_tokens[0][0])
    user = _opted_in(db, admin)
    for market in (market_a, market_b):
        h.split(db, admin, user, market, 10_000_000)
    h.resolve_yes(db, admin, market_a, market_b)

    assert redeem_resolved_markets(db, admin, capped) == 1
    assert _market_row(db, market_a.market_id).fully_redeemed is True
    assert _market_row(db, market_b.market_id).fully_redeemed is False
    assert admin.ctf_balance(user.eth_address, yes_b) == 10_000_000

    assert redeem_resolved_markets(db, admin, capped) == 1
    assert _market_row(db, market_b.market_id).fully_redeemed is True
    assert admin.ctf_balance(user.eth_address, yes_b) == 0


def test_only_dust_and_losing_tokens_left_settle_the_market_without_a_transaction(admin, db):
    """Neither holder is owed $0.01 (dust; only the loser): nothing sent, the market flagged."""
    settings = Settings().model_copy(update={"min_claim_micro": 10_000})
    market = h.synced_market(db, admin)
    yes_token, no_token = (int(t) for t, _label in market.erc1155_tokens)
    dust, loser = _opted_in(db, admin), _opted_in(db, admin)
    h.split(db, admin, dust, market, 5_000)  # 0.005 apUSD of each side
    h.split(db, admin, loser, market, 20_000_000)
    h.give_tokens(admin, loser, Account.create().address, yes_token, 20_000_000)  # keeps only NO
    h.resolve_yes(db, admin, market)
    for holder in (dust, loser):
        h.drain_native_balance(admin, holder.eth_address)
    nonces = {u.eth_address: admin.transaction_count(u.eth_address) for u in (dust, loser)}

    assert redeem_resolved_markets(db, admin, settings) == 0

    for holder in (dust, loser):  # nothing sent from either, and never topped up
        assert admin.transaction_count(holder.eth_address) == nonces[holder.eth_address]
        assert admin.native_balance(holder.eth_address) == 0
    assert admin.ctf_balance(dust.eth_address, yes_token) == 5_000
    assert admin.ctf_balance(loser.eth_address, no_token) == 20_000_000
    assert _market_row(db, market.market_id).fully_redeemed is True
