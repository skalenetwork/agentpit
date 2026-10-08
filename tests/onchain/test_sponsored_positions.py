"""Split, merge and claim from a wallet holding no native coin, on anvil.

Every user transaction is paid for by `UserGasSponsor`: just before the call,
the admin sends the wallet exactly what it needs. These tests check that on the
real chain:

- the top-up is exact;
- the wallet never keeps more than one action's need;
- a claim with nothing worth claiming costs nobody a transaction;
- the sponsored gas lands in the account's daily `sponsored_gas` row;
- a claim is logged at the CTF's payout, whatever else moves the wallet's
  apUSD while the claim is in flight.

Each branch of the gate is pinned with fakes in
tests/services/test_position_service.py.
"""

from __future__ import annotations

import json
import secrets
import time

import pytest
from fastapi.testclient import TestClient
from web3 import Web3
from web3.logs import DISCARD

from agentpit.api.app import create_app
from agentpit.api.deps import get_db_session, get_onchain_admin
from agentpit.config import Settings
from agentpit.datastructures.market import Market
from agentpit.datastructures.split_position_request import (
    MergePositionRequest,
    SplitPositionRequest,
)
from agentpit.datastructures.user import User
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import MarketStateError, NothingToClaimError
from agentpit.onchain.admin import OnchainAdmin
from agentpit.onchain.contracts import Contracts
from agentpit.onchain.deployment import Deployment
from agentpit.onchain.tx_sender import TRANSFER_GAS
from agentpit.onchain.user_wallet import send_user_tx
from agentpit.onchain.web3_client import Web3Client
from agentpit.polymarket.polymarket_sync import (
    create_polymarket_markets_if_needed,
    mirror_polymarket_resolutions,
)
from agentpit.services.account_service import AccountService
from agentpit.services.gas_sponsor import UserGasSponsor
from agentpit.utils.parse import hex2bytes
from tests.db_helpers import fresh_test_db
from tests.onchain._helpers import drain_native_balance, hdr, position_service

_PARTITION = [1, 2]


def _chain() -> tuple[OnchainAdmin, DbSession]:
    """An admin on the local deployment and a pool on the test database."""
    settings = Settings()
    deployment = Deployment.load(settings.deployment_path)
    client = Web3Client(settings, deployment)
    return OnchainAdmin(client, Contracts(client.web3, deployment)), fresh_test_db()


def _market(db: DbSession, admin: OnchainAdmin) -> tuple[Market, dict]:
    """A binary market prepared on the local CTF and ACTIVE, plus the upstream
    document it was synced from (what `_resolve` later mirrors)."""
    suffix = secrets.token_hex(4)
    pm = {
        "id": int(secrets.token_hex(4), 16),
        "conditionId": "0x" + secrets.token_hex(32),
        "question": f"Sponsored claim {suffix}?",
        "description": "d",
        "slug": f"sponsored-claim-{suffix}",
        "startDate": "2020-01-01T00:00:00Z",
        "endDate": "2020-01-02T00:00:00Z",
        "active": True,
        "closed": False,
        "tokens": [
            {"token_id": str(int(secrets.token_hex(8), 16)), "outcome": "Yes"},
            {"token_id": str(int(secrets.token_hex(8), 16)), "outcome": "No"},
        ],
    }
    with db.write() as conn:
        market = create_polymarket_markets_if_needed(conn, [pm], admin)[0]
    return market, pm


def _resolve(db: DbSession, admin: OnchainAdmin, market: Market, pm: dict, winner: int) -> None:
    """Mirror an upstream win: `reportPayouts` on chain, then RESOLVED in the DB."""
    upstream = dict(
        pm,
        closed=True,
        tokens=[dict(t, winner=(i == winner)) for i, t in enumerate(pm["tokens"])],
    )
    with db.write() as conn:
        mirror_polymarket_resolutions(
            conn,
            admin,
            fetcher=lambda _cid: upstream,
            now=9_999_999_999,
            market_ids={market.market_id},
        )


def _account(db: DbSession) -> User:
    """A fresh account that has never held native coin or sent a transaction."""
    with db.write() as conn:
        user_id, _acct, _key = TableWrite.create_user(
            conn,
            email=f"sponsor-{secrets.token_hex(4)}@example.com",
            password_hash="x",
            handle=None,
        )
    with db.read() as conn:
        user = TableRead.get_user_by_userid(conn, user_id)
    assert user is not None
    return user


def _holder(db: DbSession, admin: OnchainAdmin) -> User:
    """`_account`, onboarded the way signup does it: the faucet's apUSD and
    the three approvals, sent after one sponsored top-up. The wallet ends up
    holding at most what those approvals needed."""
    user = _account(db)
    admin.faucet_drip(user.eth_address)
    sponsor = UserGasSponsor(db, admin, Settings())
    with sponsor.locked(user):
        sponsor.send(user, admin.approval_calls(), "onboarding")
    return user


def _need(admin: OnchainAdmin, fn, address: str) -> int:
    """What the sponsor sizes `fn` at: the fee-less estimate plus 20%, at the
    current gas price. Exact when nothing is mined between this and the send."""
    return admin.estimate_user_gas(fn, address) * 120 // 100 * admin.gas_price()


def _last_receipt(admin: OnchainAdmin, address: str):
    """The receipt of `address`'s latest transaction. The service does not
    return it. anvil mines each transaction in a block of its own, so the
    transaction is in one of the last few blocks."""
    web3 = admin._client.web3  # noqa: SLF001
    sender = Web3.to_checksum_address(address)
    head = web3.eth.block_number
    for number in range(head, max(head - 8, -1), -1):
        block = web3.eth.get_block(number, full_transactions=True)
        for tx in reversed(block["transactions"]):
            if tx["from"] == sender:
                return web3.eth.get_transaction_receipt(tx["hash"])
    raise AssertionError(f"no recent transaction from {sender}")


def _spent(receipt) -> int:
    return receipt["gasUsed"] * receipt["effectiveGasPrice"]


def _booked(db: DbSession, user: User) -> int:
    with db.read() as conn:
        return TableRead.sponsored_gas_used(
            conn, user.api_key, int(time.time()) // 86_400
        )


def _rows(db: DbSession, user: User, kind: str) -> int:
    with db.read() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS N FROM transactions "
            "WHERE API_KEY = %s AND TRANSACTION_TYPE = %s",
            (user.api_key, kind),
        ).fetchone()["N"]


def _gift(admin: OnchainAdmin, sender: User, recipient: User, token_id: int, amount: int) -> None:
    """Move `amount` of one outcome token from `sender` to `recipient`. The
    sender signs it and pays its gas directly; the recipient never transacts."""
    admin.fund_gas(sender.eth_address, 10**15)
    fn = admin._contracts.ctf.functions.safeTransferFrom(  # noqa: SLF001
        Web3.to_checksum_address(sender.eth_address),
        Web3.to_checksum_address(recipient.eth_address),
        token_id,
        amount,
        b"",
    )
    assert send_user_tx(admin._client, sender.eth_key, fn)["status"] == 1  # noqa: SLF001


def _refused_without_a_transaction(
    db: DbSession, admin: OnchainAdmin, user: User, market_id: int, error, text: str
) -> None:
    """Claim, expect `error`, and prove nothing was sent. Checks that:

    - neither the user's nonce nor the admin's moved;
    - neither balance changed;
    - nothing was booked;
    - no REDEEM row was written.
    """
    wallet, payer = user.eth_address, admin.oracle_address

    def snapshot():
        return (
            admin.transaction_count(wallet),
            admin.native_balance(wallet),
            admin.transaction_count(payer),
            admin.native_balance(payer),
            _booked(db, user),
        )

    before = snapshot()
    with pytest.raises(error, match=text):
        position_service(db, admin).redeem(user, market_id)
    assert snapshot() == before
    assert _rows(db, user, "REDEEM") == 0


def test_a_claim_from_an_empty_wallet_is_topped_up_exactly_and_pays_out():
    admin, db = _chain()
    market, pm = _market(db, admin)
    user = _holder(db, admin)
    positions = position_service(db, admin)
    positions.split(user, market.market_id, SplitPositionRequest(amount=100_000_000))
    _resolve(db, admin, market, pm, winner=0)  # YES wins
    drain_native_balance(admin, user.eth_address)
    assert admin.native_balance(user.eth_address) == 0

    cid = hex2bytes(market.condition_id.value)
    need = _need(admin, admin.redeem_call(cid, _PARTITION), user.eth_address)
    usd_before, booked_before = admin.usd_balance(user.eth_address), _booked(db, user)

    claimed = positions.redeem(user, market.market_id)

    tokens = [int(t) for t, _label in market.erc1155_tokens]
    assert claimed.collateral_amount == 100_000_000
    assert admin.usd_balance(user.eth_address) == usd_before + 100_000_000
    assert admin.ctf_balances(user.eth_address, tokens) == [0, 0]  # the loser burned too
    receipt = _last_receipt(admin, user.eth_address)
    assert receipt["status"] == 1
    # Topped up from 0 to exactly the need; the claim spent part of it.
    assert admin.native_balance(user.eth_address) + _spent(receipt) == need
    # Never refused for budget, but booked: the top-up's transfer plus the claim.
    assert _booked(db, user) - booked_before == TRANSFER_GAS + receipt["gasUsed"]
    assert _rows(db, user, "REDEEM") == 1


def test_consecutive_claims_never_leave_more_than_one_claims_need():
    """The balance cannot creep. A top-up happens only when the wallet holds
    less than the next claim needs, and it brings the wallet to exactly that.
    So after each claim the wallet holds at most the largest need so far. On
    anvil it keeps almost all of it, because anvil bills the base fee, not
    maxFeePerGas."""
    admin, db = _chain()
    user = _holder(db, admin)
    positions = position_service(db, admin)
    markets = [_market(db, admin) for _ in range(3)]
    for market, _pm in markets:
        positions.split(user, market.market_id, SplitPositionRequest(amount=20_000_000))
    for market, pm in markets:
        _resolve(db, admin, market, pm, winner=0)
    drain_native_balance(admin, user.eth_address)

    needs: list[int] = []
    for market, _pm in markets:
        cid = hex2bytes(market.condition_id.value)
        needs.append(_need(admin, admin.redeem_call(cid, _PARTITION), user.eth_address))
        before = admin.native_balance(user.eth_address)

        assert positions.redeem(user, market.market_id).collateral_amount == 20_000_000

        after = admin.native_balance(user.eth_address)
        # Topped up to exactly this claim's need, and only when short of it.
        assert after + _spent(_last_receipt(admin, user.eth_address)) == max(before, needs[-1])
        assert after <= max(needs)


@pytest.mark.parametrize(
    "gift",
    [
        pytest.param(None, id="zero-holdings"),
        pytest.param((1, 50_000_000), id="losing-tokens-only"),
        pytest.param((0, 9_999), id="dust-below-a-cent"),
    ],
)
def test_nothing_worth_claiming_is_refused_without_a_transaction(gift):
    """`redeemPositions` succeeds with nothing to redeem, so without the gate
    each of these would cost the admin a top-up and the account a claim."""
    admin, db = _chain()
    market, pm = _market(db, admin)
    whale = _holder(db, admin)
    position_service(db, admin).split(
        whale, market.market_id, SplitPositionRequest(amount=100_000_000)
    )
    claimant = _account(db)
    if gift is not None:
        index, amount = gift
        _gift(admin, whale, claimant, int(market.erc1155_tokens[index][0]), amount)
    _resolve(db, admin, market, pm, winner=0)  # YES wins
    assert admin.native_balance(claimant.eth_address) == 0  # a claim would need a top-up

    _refused_without_a_transaction(
        db, admin, claimant, market.market_id, NothingToClaimError, "nothing to claim"
    )


def test_a_market_the_chain_has_not_resolved_is_refused_without_a_transaction():
    """RESOLVED in the database with no `reportPayouts` on chain:
    `redeemPositions` would revert, after the admin had paid for the top-up."""
    admin, db = _chain()
    market, _pm = _market(db, admin)
    user = _holder(db, admin)
    position_service(db, admin).split(
        user, market.market_id, SplitPositionRequest(amount=100_000_000)
    )
    with db.write() as conn:
        TableWrite.resolve_market(conn, market_id=market.market_id, winning_outcome_index=0)
    drain_native_balance(admin, user.eth_address)
    assert admin.payout_vector(hex2bytes(market.condition_id.value))[0] == 0

    _refused_without_a_transaction(
        db, admin, user, market.market_id, MarketStateError, "not resolved on chain"
    )


def test_split_and_merge_from_an_empty_wallet_are_topped_up_and_booked():
    admin, db = _chain()
    market, _pm = _market(db, admin)
    user = _holder(db, admin)
    positions = position_service(db, admin)
    cid = hex2bytes(market.condition_id.value)
    tokens = [int(t) for t, _label in market.erc1155_tokens]

    drain_native_balance(admin, user.eth_address)
    need = _need(admin, admin.split_call(cid, _PARTITION, 40_000_000), user.eth_address)
    booked = _booked(db, user)
    positions.split(user, market.market_id, SplitPositionRequest(amount=40_000_000))
    receipt = _last_receipt(admin, user.eth_address)
    assert receipt["status"] == 1
    assert admin.native_balance(user.eth_address) + _spent(receipt) == need
    # The reservation (limit + transfer) trued up to what was actually spent.
    assert _booked(db, user) - booked == TRANSFER_GAS + receipt["gasUsed"]
    assert admin.ctf_balances(user.eth_address, tokens) == [40_000_000, 40_000_000]

    drain_native_balance(admin, user.eth_address)
    need = _need(admin, admin.merge_call(cid, _PARTITION, 15_000_000), user.eth_address)
    booked = _booked(db, user)
    positions.merge(user, market.market_id, MergePositionRequest(amount=15_000_000))
    receipt = _last_receipt(admin, user.eth_address)
    assert receipt["status"] == 1
    assert admin.native_balance(user.eth_address) + _spent(receipt) == need
    assert _booked(db, user) - booked == TRANSFER_GAS + receipt["gasUsed"]
    assert admin.ctf_balances(user.eth_address, tokens) == [25_000_000, 25_000_000]

    assert _rows(db, user, "SPLIT") == 1
    assert _rows(db, user, "MERGE") == 1


def test_with_the_kill_switch_off_a_dry_wallet_gets_402_not_500():
    """AGENTPIT_SPONSOR_USER_GAS=false: nothing is topped up, so the node
    refuses the claim for want of gas (anvil says "insufficient funds for
    gas"). The caller gets 402, the one status that means "the wallet could
    not pay", not a 500 from a raw RPC error."""
    client = TestClient(
        create_app(Settings(sponsor_user_gas=False)), raise_server_exceptions=False
    )
    admin = client.app.dependency_overrides[get_onchain_admin]()  # type: ignore[attr-defined]
    db = client.app.dependency_overrides[get_db_session]()  # type: ignore[attr-defined]
    market, pm = _market(db, admin)
    user = _holder(db, admin)
    # Set up with the environment's sponsor: the switch is off only in this app.
    position_service(db, admin).split(
        user, market.market_id, SplitPositionRequest(amount=100_000_000)
    )
    _resolve(db, admin, market, pm, winner=0)
    drain_native_balance(admin, user.eth_address)
    tokens = [int(t) for t, _label in market.erc1155_tokens]
    nonce, booked = admin.transaction_count(user.eth_address), _booked(db, user)

    r = client.post(
        f"/markets/{market.market_id}/redeem_position", headers=hdr(user.api_key)
    )

    assert r.status_code == 402, r.text
    assert admin.native_balance(user.eth_address) == 0  # nothing was topped up
    assert admin.transaction_count(user.eth_address) == nonce  # the node took nothing
    assert admin.ctf_balances(user.eth_address, tokens) == [100_000_000, 100_000_000]
    assert _booked(db, user) == booked  # the switch stops booking as well
    assert _rows(db, user, "REDEEM") == 0


def _redeem_amounts(db: DbSession, user: User) -> list[int]:
    """`collateral_amount` of each REDEEM row: what the profile page reads."""
    with db.read() as conn:
        return [
            json.loads(r["DETAILS"])["collateral_amount"]
            for r in conn.execute(
                "SELECT DETAILS FROM transactions "
                "WHERE API_KEY = %s AND TRANSACTION_TYPE = 'REDEEM'",
                (user.api_key,),
            ).fetchall()
        ]


def _move_apusd_while_topping_up(
    monkeypatch, admin: OnchainAdmin, user: User, *, credit: int = 0, debit: int = 0
) -> None:
    """Change the user's apUSD between the claim's gate and the claim itself.

    The sponsor tops the wallet up just before it sends, so a hook on
    `fund_gas` runs inside the claim, under the user's lock, as a fill, a
    `/me/top-up` mint or a transfer out can at any time (none of those take
    the lock). A `credit` is minted to the wallet. A `debit` is sent by the
    user itself, on gas it is given for the purpose, after the sponsored
    top-up has landed.
    """
    real = admin.fund_gas
    me = user.eth_address.lower()
    elsewhere = Web3.to_checksum_address("0x" + "5e" * 20)

    def fund_gas(address, value_wei, **kwargs):
        receipt = real(address, value_wei, **kwargs)
        if address.lower() == me:
            if credit:
                admin.mint_to(address, credit)
            if debit:
                real(address, 10**16)  # spare gas for the user's own transfer
                transfer = admin._contracts.usd.functions.transfer(  # noqa: SLF001
                    elsewhere, debit
                )
                assert send_user_tx(admin._client, user.eth_key, transfer)["status"] == 1  # noqa: SLF001
        return receipt

    monkeypatch.setattr(admin, "fund_gas", fund_gas)


@pytest.mark.parametrize(
    ("credit", "debit"),
    [
        pytest.param(7_000_000, 0, id="a-mint-of-7-lands-during-the-claim"),
        pytest.param(0, 130_000_000, id="a-transfer-of-130-leaves-during-the-claim"),
    ],
)
def test_a_claim_is_logged_at_the_ctf_payout_whatever_the_wallet_does_meanwhile(
    monkeypatch, credit, debit
):
    """A difference of two balance reads was wrong in both directions: a credit
    during the claim made it read 107, and a debit larger than the payout made
    it read -30, which `list_closed_positions` takes for a lost market and
    drops. The amount is what `redeemPositions` paid, from the receipt."""
    admin, db = _chain()
    market, pm = _market(db, admin)
    user = _holder(db, admin)
    positions = position_service(db, admin)
    positions.split(user, market.market_id, SplitPositionRequest(amount=100_000_000))
    _resolve(db, admin, market, pm, winner=0)  # YES wins
    drain_native_balance(admin, user.eth_address)  # a top-up is certain
    _move_apusd_while_topping_up(monkeypatch, admin, user, credit=credit, debit=debit)

    claimed = positions.redeem(user, market.market_id)

    receipt = _last_receipt(admin, user.eth_address)
    assert receipt["status"] == 1
    paid = admin._contracts.ctf.events.PayoutRedemption().process_receipt(  # noqa: SLF001
        receipt, errors=DISCARD
    )[0]["args"]["payout"]
    assert paid == 100_000_000
    assert claimed.collateral_amount == paid
    assert _redeem_amounts(db, user) == [paid]
    assert claimed.new_usdc_balance == admin.usd_balance(user.eth_address)

    closed = AccountService(db, admin).list_closed_positions(user.eth_address)
    assert len(closed) == 1
    assert closed[0].curPrice == 1.0
    assert closed[0].currentValue == 100.0
