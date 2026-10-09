"""End-to-end auto-redeem on the local CTF: sync a binary market, onboard a user,
split, mirror an upstream resolution, run the pass. No wallet is ever granted
gas: the approvals, splits and claims all go through `UserGasSponsor`, as in
production since 2026-10-08.
"""

from functools import partial

from eth_account import Account

from agentpit.config import Settings
from agentpit.datastructures.market_state import MarketState
from agentpit.db.table_read import TableRead
from agentpit.onchain.tx_sender import TRANSFER_GAS
from agentpit.polymarket.polymarket_sync import auto_redeem_resolved_markets
from tests.onchain._helpers import (
    chain,
    drain_native_balance,
    give_tokens,
    onboarded_account,
    resolve_yes,
    split,
    sponsored_gas,
    synced_market,
)

# Opted in explicitly, so the tests do not depend on the AUTO_REDEEM_ENABLED
# column default; `auto_redeem=False` proves an opted-out holder keeps their tokens.
_opted_in = partial(onboarded_account, auto_redeem=True)


def _market_row(db, market_id: int):
    with db.read() as conn:
        row = TableRead.read_market(conn, market_id)
    assert row is not None
    return row


def test_auto_redeem_pays_winner_and_flags_market():
    admin, db = chain()
    market, pm = synced_market(db, admin)
    mid = market.market_id
    yes_token, no_token = (int(t) for t, _label in market.erc1155_tokens)
    user = _opted_in(db, admin)
    split(db, admin, user, market, 100_000_000)  # 100 apUSD raw
    bal_before = admin.usd_balance(user.eth_address)
    assert admin.ctf_balance(user.eth_address, yes_token) == 100_000_000
    assert admin.ctf_balance(user.eth_address, no_token) == 100_000_000
    resolve_yes(db, admin, pm)

    assert auto_redeem_resolved_markets(db, admin, Settings()) == 1

    assert admin.usd_balance(user.eth_address) - bal_before == 100_000_000  # winner paid, loser 0
    assert admin.ctf_balance(user.eth_address, yes_token) == 0
    assert admin.ctf_balance(user.eth_address, no_token) == 0
    row = _market_row(db, mid)
    assert row.market_state == MarketState.RESOLVED
    assert row.fully_redeemed is True
    # Idempotent: a second pass redeems nobody.
    assert auto_redeem_resolved_markets(db, admin, Settings()) == 0


def test_auto_redeem_claims_for_a_holder_with_no_native_balance():
    """The point of gasless claims: a winner whose wallet holds 0 native is
    paid all the same, the gas booked to the account's day."""
    admin, db = chain()
    settings = Settings()
    market, pm = synced_market(db, admin)
    yes_token = int(market.erc1155_tokens[0][0])
    user = _opted_in(db, admin)
    split(db, admin, user, market, 50_000_000)
    resolve_yes(db, admin, pm)
    drain_native_balance(admin, user.eth_address)
    assert admin.native_balance(user.eth_address) == 0
    usd_before, booked_before = admin.usd_balance(user.eth_address), sponsored_gas(db, user)

    assert auto_redeem_resolved_markets(db, admin, settings) == 1

    assert admin.usd_balance(user.eth_address) - usd_before == 50_000_000
    assert admin.ctf_balance(user.eth_address, yes_token) == 0
    assert _market_row(db, market.market_id).fully_redeemed is True
    # The top-up's own transfer plus the claim's receipt gas.
    assert sponsored_gas(db, user) - booked_before > TRANSFER_GAS
    # Whatever is left is part of one claim's need, far under the ceiling.
    assert admin.native_balance(user.eth_address) <= settings.max_topup_gas * admin.gas_price()


def test_a_holder_who_has_not_opted_in_keeps_their_tokens():
    """Against the real chain, not a stub: an opted-out holder is skipped
    outright. No transaction from their wallet, tokens and balance untouched,
    the market not flagged FULLY_REDEEMED: the winnings just wait."""
    admin, db = chain()
    market, pm = synced_market(db, admin)
    yes_token, no_token = (int(t) for t, _label in market.erc1155_tokens)
    user = onboarded_account(db, admin, auto_redeem=False)
    split(db, admin, user, market, 100_000_000)
    bal_before = admin.usd_balance(user.eth_address)
    assert admin.ctf_balance(user.eth_address, yes_token) == 100_000_000
    assert admin.ctf_balance(user.eth_address, no_token) == 100_000_000
    resolve_yes(db, admin, pm)
    nonce_before = admin.transaction_count(user.eth_address)

    assert auto_redeem_resolved_markets(db, admin, Settings()) == 0

    assert admin.transaction_count(user.eth_address) == nonce_before
    assert admin.usd_balance(user.eth_address) == bal_before
    assert admin.ctf_balance(user.eth_address, yes_token) == 100_000_000
    assert admin.ctf_balance(user.eth_address, no_token) == 100_000_000
    row = _market_row(db, market.market_id)
    assert row.market_state == MarketState.RESOLVED
    assert row.fully_redeemed is False


def test_auto_redeem_stops_at_the_per_pass_cap():
    """With a cap of one claim per pass, a holder owed in two markets is paid
    in the first; the second stays open, untouched, and the next pass pays it."""
    admin, db = chain()
    capped = Settings().model_copy(update={"auto_redeem_max_per_pass": 1})
    (market_a, pm_a), (market_b, pm_b) = synced_market(db, admin), synced_market(db, admin)
    yes_b = int(market_b.erc1155_tokens[0][0])
    user = _opted_in(db, admin)
    for market in (market_a, market_b):
        split(db, admin, user, market, 10_000_000)
    resolve_yes(db, admin, pm_a, pm_b)

    assert auto_redeem_resolved_markets(db, admin, capped) == 1
    assert _market_row(db, market_a.market_id).fully_redeemed is True
    assert _market_row(db, market_b.market_id).fully_redeemed is False
    assert admin.ctf_balance(user.eth_address, yes_b) == 10_000_000

    assert auto_redeem_resolved_markets(db, admin, capped) == 1
    assert _market_row(db, market_b.market_id).fully_redeemed is True
    assert admin.ctf_balance(user.eth_address, yes_b) == 0


def test_only_dust_and_losing_tokens_left_settle_the_market_without_a_transaction():
    """Two opted-in holders, neither owed $0.01: one holds 0.005 apUSD of the
    winner, the other only the loser. The pass sends nothing for either (no
    top-up, no claim) and still flags the market: nothing worth claiming is left."""
    admin, db = chain()
    settings = Settings().model_copy(update={"min_claim_micro": 10_000})
    market, pm = synced_market(db, admin)
    yes_token, no_token = (int(t) for t, _label in market.erc1155_tokens)
    dust, loser = _opted_in(db, admin), _opted_in(db, admin)
    split(db, admin, dust, market, 5_000)  # 0.005 apUSD of each side
    split(db, admin, loser, market, 20_000_000)
    give_tokens(admin, loser, Account.create().address, yes_token, 20_000_000)  # keeps only NO
    resolve_yes(db, admin, pm)
    for holder in (dust, loser):
        drain_native_balance(admin, holder.eth_address)
    nonces = {h.eth_address: admin.transaction_count(h.eth_address) for h in (dust, loser)}

    assert auto_redeem_resolved_markets(db, admin, settings) == 0

    for holder in (dust, loser):
        assert admin.transaction_count(holder.eth_address) == nonces[holder.eth_address]  # nothing sent
        assert admin.native_balance(holder.eth_address) == 0  # never topped up
    assert admin.ctf_balance(dust.eth_address, yes_token) == 5_000
    assert admin.ctf_balance(loser.eth_address, no_token) == 20_000_000
    assert _market_row(db, market.market_id).fully_redeemed is True
