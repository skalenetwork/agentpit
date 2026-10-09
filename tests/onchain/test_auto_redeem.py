"""End-to-end auto-redeem on the local CTF.

Sync a binary market, onboard a user, split to give them both outcome tokens,
mirror an upstream resolution (reportPayouts on-chain + RESOLVED), then run
auto-redeem and assert the winner paid out, tokens are burned, and the market
is flagged FULLY_REDEEMED.
"""

import secrets

import pytest

from agentpit.datastructures.market_state import MarketState
from agentpit.datastructures.split_position_request import SplitPositionRequest
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import InsufficientGasError
from agentpit.polymarket.polymarket_sync import UpstreamMarket, parse
from agentpit.services.market_service import (
    create_markets,
    pay_out,
    redeem_resolved_markets,
)
from agentpit.services.position_service import PositionService
from tests.chain_fakes import gamma_row


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


def _pm(question_suffix: str) -> UpstreamMarket:
    m = parse(
        gamma_row(
            question=f"Auto redeem {question_suffix}?",
            startDate="2020-01-01T00:00:00Z",
            endDate="2020-01-02T00:00:00Z",
        )
    )
    assert isinstance(m, UpstreamMarket)
    return m


def _drain_native_balance(client, user_account) -> None:
    """Zero out `user_account`'s native balance -- the state I4's
    `InsufficientGasError` path exercises.

    A real send-to-zero is fussy to get exact (EIP-1559's effective gas
    price is only known after the block mines, so a transfer sized off
    `maxFeePerGas` overpays and leaves dust behind). This is a local anvil
    chain, so use its `anvil_setBalance` cheat method instead -- exact, and
    it's the balance-manipulation tool the chain itself provides for tests.
    """
    client.web3.provider.make_request("anvil_setBalance", [user_account.address, "0x0"])


def test_auto_redeem_pays_winner_and_flags_market():
    admin, db = _build_admin_and_db()
    pm = _pm(secrets.token_hex(4))

    created = create_markets(db, admin, [pm])
    market = created[0]
    mid = market.market_id
    yes_token = int(market.erc1155_tokens[0][0])
    no_token = int(market.erc1155_tokens[1][0])

    user = _onboard_user(db, admin)
    assert user is not None
    split_amount = 100_000_000  # 100 apUSD raw
    PositionService(db, admin).split(user, mid, SplitPositionRequest(amount=split_amount))

    bal_before = admin.usd_balance(user.eth_address)
    assert admin.ctf_balance(user.eth_address, yes_token) == split_amount
    assert admin.ctf_balance(user.eth_address, no_token) == split_amount

    pay_out(db, admin, {mid: (1, 0)})

    redeemed = redeem_resolved_markets(db, admin, 10)
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
    assert redeem_resolved_markets(db, admin, 10) == 0


def test_a_holder_who_has_not_opted_in_keeps_their_tokens():
    """The other half of the guarantee, proven against the real chain rather
    than a stub: a holder who never turned auto-redeem on is skipped
    outright. Their tokens are not moved and their balance is not touched;
    the market is still done, since each one is auto-redeemed once."""
    admin, db = _build_admin_and_db()
    pm = _pm(secrets.token_hex(4))

    created = create_markets(db, admin, [pm])
    market = created[0]
    mid = market.market_id
    yes_token = int(market.erc1155_tokens[0][0])
    no_token = int(market.erc1155_tokens[1][0])

    user = _onboard_user(db, admin, auto_redeem=False)
    assert user is not None
    split_amount = 100_000_000  # 100 apUSD raw
    PositionService(db, admin).split(user, mid, SplitPositionRequest(amount=split_amount))

    bal_before = admin.usd_balance(user.eth_address)
    assert admin.ctf_balance(user.eth_address, yes_token) == split_amount
    assert admin.ctf_balance(user.eth_address, no_token) == split_amount

    pay_out(db, admin, {mid: (1, 0)})

    assert redeem_resolved_markets(db, admin, 10) == 0

    # Nothing moved.
    assert admin.usd_balance(user.eth_address) == bal_before
    assert admin.ctf_balance(user.eth_address, yes_token) == split_amount
    assert admin.ctf_balance(user.eth_address, no_token) == split_amount

    with db.read() as conn:
        row = TableRead.read_market(conn, mid)
    assert row is not None
    assert row.market_state == MarketState.RESOLVED
    assert row.fully_redeemed is True


def test_claiming_with_no_gas_raises_a_domain_error_not_a_crash():
    """I4: `send_user_tx`'s broadcast fails against the *real* node with
    web3's `Web3RPCError` ("Insufficient funds for gas * price + value",
    code -32003) when the sender can't cover gas. `PositionService.redeem`
    must turn that into `InsufficientGasError` naming what's missing and
    where to send it -- not let the raw RPC exception escape toward a 500."""
    admin, db = _build_admin_and_db()
    pm = _pm(secrets.token_hex(4))

    created = create_markets(db, admin, [pm])
    market = created[0]
    mid = market.market_id

    user = _onboard_user(db, admin)
    assert user is not None
    split_amount = 100_000_000  # 100 apUSD raw
    PositionService(db, admin).split(
        user, mid, SplitPositionRequest(amount=split_amount)
    )

    pay_out(db, admin, {mid: (1, 0)})

    _drain_native_balance(admin._client, user.eth_key)  # noqa: SLF001
    assert admin.native_balance(user.eth_address) == 0

    with pytest.raises(InsufficientGasError) as exc_info:
        PositionService(db, admin).redeem(user, mid)

    # Names what's missing (gas) and where to send it (the user's own
    # address) -- not just "it failed".
    message = str(exc_info.value)
    assert "gas" in message
    assert user.eth_address in message

    # Nothing moved: the failed send never got far enough to burn tokens or
    # pay out collateral.
    yes_token = int(market.erc1155_tokens[0][0])
    assert admin.ctf_balance(user.eth_address, yes_token) == split_amount
