"""End-to-end auto-redeem on the local CTF.

Sync a binary market, onboard a user, split to give them both outcome tokens,
mirror an upstream resolution (reportPayouts on-chain + RESOLVED), then run
auto-redeem and assert the winner paid out, tokens are burned, and the market
is flagged FULLY_REDEEMED.

No wallet here is ever granted gas. The approvals, the splits and the claims
all go through `UserGasSponsor`, which tops each wallet up to exactly what the
transaction needs, the way production does since 2026-10-08.
"""

import secrets
import time

from eth_account import Account
from web3 import Web3

from agentpit.config import Settings
from agentpit.datastructures.market_state import MarketState
from agentpit.datastructures.split_position_request import SplitPositionRequest
from agentpit.datastructures.user import User
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.onchain.tx_sender import TRANSFER_GAS
from agentpit.onchain.user_wallet import send_user_tx
from agentpit.polymarket.polymarket_sync import (
    auto_redeem_resolved_markets,
    create_polymarket_markets_if_needed,
    mirror_polymarket_resolutions,
)
from agentpit.services.gas_sponsor import UserGasSponsor
from agentpit.services.position_service import PositionService
from tests.onchain._helpers import drain_native_balance


def _build():
    """`(admin, db, settings)` on the local anvil and the test database."""
    from agentpit.onchain.admin import OnchainAdmin
    from agentpit.onchain.contracts import Contracts
    from agentpit.onchain.deployment import Deployment
    from agentpit.onchain.web3_client import Web3Client
    from tests.db_helpers import fresh_test_db

    s = Settings()
    d = Deployment.load(s.deployment_path)
    w = Web3Client(s, d)
    c = Contracts(w.web3, d)
    return OnchainAdmin(w, c), fresh_test_db(), s


def _onboard_user(db, admin, settings: Settings, *, auto_redeem: bool = True) -> User:
    """A fresh account onboarded the way AuthService does it without a gas
    grant: apUSD from the faucet, then the three approvals sent through the
    sponsor from an empty wallet.

    The opt-in is set explicitly either way, so these tests do not depend on
    the AUTO_REDEEM_ENABLED column default. `auto_redeem=False` is for proving
    that a holder who opted out keeps their tokens.
    """
    email = f"redeem-{secrets.token_hex(4)}@example.com"
    with db.write() as conn:
        user_id, acct, _api_key = TableWrite.create_user(
            conn, email=email, password_hash="x", handle=None
        )
        TableWrite.set_auto_redeem(conn, user_id, auto_redeem)
    with db.read() as conn:
        user = TableRead.get_user_by_userid(conn, user_id)
    assert user is not None
    admin.faucet_drip(acct.address)
    sponsor = UserGasSponsor(db, admin, settings)
    with sponsor.locked(user):
        sponsor.send(user, admin.approval_calls(), "onboarding")
    return user


def _split(db, admin, settings: Settings, user: User, market_id: int, amount: int):
    sponsor = UserGasSponsor(db, admin, settings)
    PositionService(db, admin, sponsor).split(
        user, market_id, SplitPositionRequest(amount=amount)
    )


def _pm(question_suffix: str) -> dict:
    return {
        "id": int(secrets.token_hex(4), 16),
        "conditionId": "0x" + secrets.token_hex(32),
        "question": f"Auto redeem {question_suffix}?",
        "description": "d",
        "slug": f"auto-redeem-{question_suffix}",
        "startDate": "2020-01-01T00:00:00Z",
        "endDate": "2020-01-02T00:00:00Z",
        "active": True,
        "closed": False,
        "tokens": [
            {"token_id": str(int(secrets.token_hex(8), 16)), "outcome": "Yes"},
            {"token_id": str(int(secrets.token_hex(8), 16)), "outcome": "No"},
        ],
    }


def _resolved(pm: dict, winner_index: int) -> dict:
    out = dict(pm)
    out["closed"] = True
    out["tokens"] = [
        dict(t, winner=(i == winner_index)) for i, t in enumerate(pm["tokens"])
    ]
    return out


def _resolve(db, admin, *pms: dict) -> None:
    """Mirror a YES win for each of `pms`: reportPayouts on chain, then
    RESOLVED in the database."""
    upstream = {pm["conditionId"]: _resolved(pm, winner_index=0) for pm in pms}
    with db.write() as conn:
        mirror_polymarket_resolutions(
            conn, admin, fetcher=upstream.get, now=9_999_999_999
        )


def _give_away(admin, holder: User, token_id: int, amount: int) -> None:
    """Send `amount` of one outcome token to a fresh address, so `holder`
    keeps only the other side. A plain ERC-1155 transfer is not something the
    sponsor sends, so its gas is granted by hand."""
    admin.fund_gas(holder.eth_address, 10**17)
    fn = admin._contracts.ctf.functions.safeTransferFrom(  # noqa: SLF001
        Web3.to_checksum_address(holder.eth_address),
        Account.create().address,
        token_id,
        amount,
        b"",
    )
    receipt = send_user_tx(admin._client, holder.eth_key, fn)  # noqa: SLF001
    assert receipt["status"] == 1


def _fully_redeemed(db, market_id: int) -> bool:
    with db.read() as conn:
        row = TableRead.read_market(conn, market_id)
    assert row is not None
    return row.fully_redeemed


def test_auto_redeem_pays_winner_and_flags_market():
    admin, db, settings = _build()
    pm = _pm(secrets.token_hex(4))

    with db.write() as conn:
        created = create_polymarket_markets_if_needed(conn, [pm], admin)
    market = created[0]
    mid = market.market_id
    yes_token = int(market.erc1155_tokens[0][0])
    no_token = int(market.erc1155_tokens[1][0])

    user = _onboard_user(db, admin, settings)
    split_amount = 100_000_000  # 100 apUSD raw
    _split(db, admin, settings, user, mid, split_amount)

    bal_before = admin.usd_balance(user.eth_address)
    assert admin.ctf_balance(user.eth_address, yes_token) == split_amount
    assert admin.ctf_balance(user.eth_address, no_token) == split_amount

    _resolve(db, admin, pm)

    redeemed = auto_redeem_resolved_markets(db, admin, settings)
    assert redeemed == 1

    bal_after = admin.usd_balance(user.eth_address)
    assert bal_after - bal_before == split_amount  # winner paid, loser 0
    assert admin.ctf_balance(user.eth_address, yes_token) == 0
    assert admin.ctf_balance(user.eth_address, no_token) == 0

    with db.read() as conn:
        row = TableRead.read_market(conn, mid)
    assert row is not None
    assert row.market_state == MarketState.RESOLVED
    assert row.fully_redeemed is True

    # Idempotent: a second pass redeems nobody.
    assert auto_redeem_resolved_markets(db, admin, settings) == 0


def test_auto_redeem_claims_for_a_holder_with_no_native_balance():
    """The point of gasless claims: a winner whose wallet holds 0 native is
    paid all the same. The sponsor tops the wallet up to the claim's need just
    before sending it, and books that gas to the account's day."""
    admin, db, settings = _build()
    pm = _pm(secrets.token_hex(4))
    with db.write() as conn:
        market = create_polymarket_markets_if_needed(conn, [pm], admin)[0]
    mid = market.market_id
    yes_token = int(market.erc1155_tokens[0][0])

    user = _onboard_user(db, admin, settings)
    split_amount = 50_000_000
    _split(db, admin, settings, user, mid, split_amount)
    _resolve(db, admin, pm)

    drain_native_balance(admin, user.eth_address)
    assert admin.native_balance(user.eth_address) == 0
    usd_before = admin.usd_balance(user.eth_address)
    day = int(time.time()) // 86_400
    with db.read() as conn:
        booked_before = TableRead.sponsored_gas_used(conn, user.api_key, day)

    assert auto_redeem_resolved_markets(db, admin, settings) == 1

    assert admin.usd_balance(user.eth_address) - usd_before == split_amount
    assert admin.ctf_balance(user.eth_address, yes_token) == 0
    assert _fully_redeemed(db, mid) is True
    with db.read() as conn:
        booked_after = TableRead.sponsored_gas_used(conn, user.api_key, day)
    # The top-up's own transfer plus the claim's receipt gas.
    assert booked_after - booked_before > TRANSFER_GAS
    # Whatever is left is part of one claim's need, far under the ceiling.
    assert admin.native_balance(user.eth_address) <= (
        settings.max_topup_gas * admin.gas_price()
    )


def test_a_holder_who_has_not_opted_in_keeps_their_tokens():
    """The other half of the guarantee, proven against the real chain rather
    than a stub: a holder who turned auto-redeem off is skipped outright.
    Their tokens are not moved, their balance is not touched, no transaction
    is sent from their wallet, and the market is not flagged FULLY_REDEEMED --
    the winnings just wait."""
    admin, db, settings = _build()
    pm = _pm(secrets.token_hex(4))

    with db.write() as conn:
        created = create_polymarket_markets_if_needed(conn, [pm], admin)
    market = created[0]
    mid = market.market_id
    yes_token = int(market.erc1155_tokens[0][0])
    no_token = int(market.erc1155_tokens[1][0])

    user = _onboard_user(db, admin, settings, auto_redeem=False)
    split_amount = 100_000_000  # 100 apUSD raw
    _split(db, admin, settings, user, mid, split_amount)

    bal_before = admin.usd_balance(user.eth_address)
    assert admin.ctf_balance(user.eth_address, yes_token) == split_amount
    assert admin.ctf_balance(user.eth_address, no_token) == split_amount

    _resolve(db, admin, pm)
    nonce_before = admin.transaction_count(user.eth_address)

    assert auto_redeem_resolved_markets(db, admin, settings) == 0

    # Nothing moved.
    assert admin.transaction_count(user.eth_address) == nonce_before
    assert admin.usd_balance(user.eth_address) == bal_before
    assert admin.ctf_balance(user.eth_address, yes_token) == split_amount
    assert admin.ctf_balance(user.eth_address, no_token) == split_amount

    with db.read() as conn:
        row = TableRead.read_market(conn, mid)
    assert row is not None
    assert row.market_state == MarketState.RESOLVED
    assert row.fully_redeemed is False


def test_auto_redeem_stops_at_the_per_pass_cap():
    """With a cap of one claim per pass, a holder owed in two markets is paid
    in the first; the second stays open, untouched, and the next pass pays
    it."""
    admin, db, settings = _build()
    capped = settings.model_copy(update={"auto_redeem_max_per_pass": 1})
    pm_a, pm_b = _pm(secrets.token_hex(4)), _pm(secrets.token_hex(4))
    with db.write() as conn:
        market_a = create_polymarket_markets_if_needed(conn, [pm_a], admin)[0]
    with db.write() as conn:
        market_b = create_polymarket_markets_if_needed(conn, [pm_b], admin)[0]
    yes_b = int(market_b.erc1155_tokens[0][0])

    user = _onboard_user(db, admin, settings)
    for market in (market_a, market_b):
        _split(db, admin, settings, user, market.market_id, 10_000_000)
    _resolve(db, admin, pm_a, pm_b)

    assert auto_redeem_resolved_markets(db, admin, capped) == 1
    assert _fully_redeemed(db, market_a.market_id) is True
    assert _fully_redeemed(db, market_b.market_id) is False
    assert admin.ctf_balance(user.eth_address, yes_b) == 10_000_000

    assert auto_redeem_resolved_markets(db, admin, capped) == 1
    assert _fully_redeemed(db, market_b.market_id) is True
    assert admin.ctf_balance(user.eth_address, yes_b) == 0


def test_only_dust_and_losing_tokens_left_settle_the_market_without_a_transaction():
    """Two opted-in holders, neither owed $0.01: one holds 0.005 apUSD of the
    winner, the other only the loser. The pass sends nothing for either -- no
    top-up, no claim -- and still flags the market, because nothing worth
    claiming is left in it."""
    admin, db, settings = _build()
    settings = settings.model_copy(update={"min_claim_micro": 10_000})
    pm = _pm(secrets.token_hex(4))
    with db.write() as conn:
        market = create_polymarket_markets_if_needed(conn, [pm], admin)[0]
    mid = market.market_id
    yes_token = int(market.erc1155_tokens[0][0])
    no_token = int(market.erc1155_tokens[1][0])

    dust = _onboard_user(db, admin, settings)
    loser = _onboard_user(db, admin, settings)
    _split(db, admin, settings, dust, mid, 5_000)  # 0.005 apUSD of each side
    _split(db, admin, settings, loser, mid, 20_000_000)
    _give_away(admin, loser, yes_token, 20_000_000)  # keeps only NO
    _resolve(db, admin, pm)  # YES wins

    for holder in (dust, loser):
        drain_native_balance(admin, holder.eth_address)
    nonces = {
        h.eth_address: admin.transaction_count(h.eth_address) for h in (dust, loser)
    }

    assert auto_redeem_resolved_markets(db, admin, settings) == 0

    for holder in (dust, loser):
        nonce = admin.transaction_count(holder.eth_address)
        assert nonce == nonces[holder.eth_address]  # nothing sent from it
        assert admin.native_balance(holder.eth_address) == 0  # never topped up
    assert admin.ctf_balance(dust.eth_address, yes_token) == 5_000
    assert admin.ctf_balance(loser.eth_address, no_token) == 20_000_000
    assert _fully_redeemed(db, mid) is True
