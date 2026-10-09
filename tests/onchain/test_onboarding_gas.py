"""Onboarding with no signup grant, on the local anvil.

`UserGasSponsor` tops a new wallet up to exactly what its three approvals need,
in one transfer, and sends them: a new account, a person's or an agent's, ends
onboarding with its approvals set and no more native than that top-up. On anvil
it keeps nearly all of it (anvil bills the 7 wei base fee, not the price the
send offered); never more than the need.
"""

import time

from agentpit.api.deps import get_db_session, get_onchain_admin, get_settings
from agentpit.db.table_read import TableRead
from agentpit.onchain.tx_sender import TRANSFER_GAS
from agentpit.services.agent_accounts import AgentAccounts
from tests.db_helpers import fresh_test_db
from tests.onchain._helpers import (
    _auth_service,
    assert_approvals_set,
    fresh_client,
    register,
)


def _record_top_ups(monkeypatch, client) -> list[tuple[str, int]]:
    """Every native transfer the app's admin is asked for, still sent for real."""
    admin = client.app.dependency_overrides[get_onchain_admin]()  # type: ignore[attr-defined]
    sent: list[tuple[str, int]] = []
    real = admin.fund_gas

    def fund_gas(user_address, value_wei, *, timeout=30):
        sent.append((user_address.lower(), value_wei))
        return real(user_address, value_wei, timeout=timeout)

    monkeypatch.setattr(admin, "fund_gas", fund_gas)
    return sent


def _assert_onboarded_on_one_exact_top_up(client, address: str, api_key: str, top_ups) -> None:
    overrides = client.app.dependency_overrides  # type: ignore[attr-defined]
    admin, settings = overrides[get_onchain_admin](), overrides[get_settings]()

    mine = [wei for to, wei in top_ups if to == address.lower()]
    # One transfer, and the sponsor's: under its AGENTPIT_MAX_TOPUP_GAS ceiling
    # at today's price (the old 0.02 native grant is twenty times that at anvil's ~1 gwei).
    assert len(mine) == 1, mine
    need = mine[0]
    assert 0 < need <= settings.max_topup_gas * admin.gas_price()
    # The wallet started empty, so the top-up was the whole need; the approvals
    # spent part of it and nothing was added afterwards.
    assert admin.native_balance(address) <= need
    # Exactly the three approvals went out from the wallet, and they are set.
    assert admin.transaction_count(address) == 3
    assert_approvals_set(admin, address)
    # The top-up and the approvals are on the account's daily row (measured
    # 46,487 / 46,487 / 45,996 gas for the approvals on anvil).
    with fresh_test_db().read() as conn:
        booked = TableRead.sponsored_gas_used(conn, api_key, int(time.time()) // 86_400)
    assert TRANSFER_GAS + 3 * 21_000 < booked < TRANSFER_GAS + 3 * 60_000


def test_a_new_account_onboards_on_one_exact_top_up_and_no_grant(monkeypatch):
    client = fresh_client()
    top_ups = _record_top_ups(monkeypatch, client)

    body = register(client)

    assert body["user"]["onboarded_at"] is not None
    _assert_onboarded_on_one_exact_top_up(client, body["user"]["eth_address"], body["api_key"], top_ups)


def test_a_new_api_agent_onboards_the_same_way(monkeypatch):
    client = fresh_client()
    top_ups = _record_top_ups(monkeypatch, client)
    db = client.app.dependency_overrides[get_db_session]()  # type: ignore[attr-defined]

    agent = AgentAccounts(db, _auth_service(client)._onboard_new_account).create_api_agent("user_onboarding_gas")

    assert agent.onboarded_at is not None
    _assert_onboarded_on_one_exact_top_up(client, agent.eth_address, agent.api_key, top_ups)
