"""End-to-end auto-redeem on the local CTF.

Sync a binary market, onboard a user, split to give them both outcome tokens,
mirror an upstream resolution (reportPayouts on-chain + RESOLVED), then run
auto-redeem and assert the winner paid out, tokens are burned, and the market
is flagged FULLY_REDEEMED.
"""

import secrets

from agentpit.datastructures.market_state import MarketState
from agentpit.datastructures.split_position_request import SplitPositionRequest
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.polymarket.polymarket_sync import (
    auto_redeem_resolved_markets,
    create_polymarket_markets_if_needed,
    mirror_polymarket_resolutions,
)
from tests.onchain._helpers import position_service


def _build_admin_and_db():
    from agentpit.config import Settings
    from agentpit.onchain.admin import OnchainAdmin
    from agentpit.onchain.contracts import Contracts
    from agentpit.onchain.deployment import Deployment
    from agentpit.onchain.web3_client import Web3Client
    from tests.db_helpers import fresh_test_db

    s = Settings()
    d = Deployment.load(s.deployment_path)
    w = Web3Client(s, d)
    c = Contracts(w.web3, d)
    return OnchainAdmin(w, c), fresh_test_db()


def _onboard_user(db, admin, *, auto_redeem: bool = True):
    """Onboard a fresh user, opted into auto-redeem by default.

    Every account starts opted out (AUTO_REDEEM_ENABLED defaults FALSE), so a
    test exercising the auto-redeem loop's happy path has to opt one in
    explicitly -- this is that. `auto_redeem=False` is for the opposite case:
    proving a holder who never opted in keeps their tokens.
    """
    email = f"redeem-{secrets.token_hex(4)}@example.com"
    with db.write() as conn:
        user_id, acct, _api_key = TableWrite.create_user(
            conn, email=email, password_hash="x", handle=None
        )
        TableWrite.set_auto_redeem(conn, user_id, auto_redeem)
    admin.fund_gas(acct.address, 10**18)
    admin.faucet_drip(acct.address)
    admin.grant_user_approvals(acct)
    with db.read() as conn:
        return TableRead.get_user_by_userid(conn, user_id)


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


def test_auto_redeem_pays_winner_and_flags_market():
    admin, db = _build_admin_and_db()
    pm = _pm(secrets.token_hex(4))

    with db.write() as conn:
        created = create_polymarket_markets_if_needed(conn, [pm], admin)
    market = created[0]
    mid = market.market_id
    yes_token = int(market.erc1155_tokens[0][0])
    no_token = int(market.erc1155_tokens[1][0])

    user = _onboard_user(db, admin)
    assert user is not None
    split_amount = 100_000_000  # 100 apUSD raw
    position_service(db, admin).split(user, mid, SplitPositionRequest(amount=split_amount))

    bal_before = admin.usd_balance(user.eth_address)
    assert admin.ctf_balance(user.eth_address, yes_token) == split_amount
    assert admin.ctf_balance(user.eth_address, no_token) == split_amount

    fake = _resolved(pm, winner_index=0)  # YES wins
    with db.write() as conn:
        mirror_polymarket_resolutions(
            conn, admin, fetcher=lambda _cid: fake, now=9_999_999_999
        )

    redeemed = auto_redeem_resolved_markets(db, admin)
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
    assert auto_redeem_resolved_markets(db, admin) == 0


def test_a_holder_who_has_not_opted_in_keeps_their_tokens():
    """The other half of the guarantee, proven against the real chain rather
    than a stub: a holder who never turned auto-redeem on is skipped
    outright. Their tokens are not moved, their balance is not touched, and
    the market is not flagged FULLY_REDEEMED -- the winnings just wait."""
    admin, db = _build_admin_and_db()
    pm = _pm(secrets.token_hex(4))

    with db.write() as conn:
        created = create_polymarket_markets_if_needed(conn, [pm], admin)
    market = created[0]
    mid = market.market_id
    yes_token = int(market.erc1155_tokens[0][0])
    no_token = int(market.erc1155_tokens[1][0])

    user = _onboard_user(db, admin, auto_redeem=False)
    assert user is not None
    split_amount = 100_000_000  # 100 apUSD raw
    position_service(db, admin).split(user, mid, SplitPositionRequest(amount=split_amount))

    bal_before = admin.usd_balance(user.eth_address)
    assert admin.ctf_balance(user.eth_address, yes_token) == split_amount
    assert admin.ctf_balance(user.eth_address, no_token) == split_amount

    fake = _resolved(pm, winner_index=0)  # YES wins
    with db.write() as conn:
        mirror_polymarket_resolutions(
            conn, admin, fetcher=lambda _cid: fake, now=9_999_999_999
        )

    assert auto_redeem_resolved_markets(db, admin) == 0

    # Nothing moved.
    assert admin.usd_balance(user.eth_address) == bal_before
    assert admin.ctf_balance(user.eth_address, yes_token) == split_amount
    assert admin.ctf_balance(user.eth_address, no_token) == split_amount

    with db.read() as conn:
        row = TableRead.read_market(conn, mid)
    assert row is not None
    assert row.market_state == MarketState.RESOLVED
    assert row.fully_redeemed is False
