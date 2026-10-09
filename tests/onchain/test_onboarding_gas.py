"""Onboarding with no signup grant, on the local anvil: `UserGasSponsor` tops a
new wallet up to exactly its three approvals' need in one transfer and sends them,
so a new account (a person's or an agent's) ends with its approvals set and no
more native than that top-up (on anvil nearly all of it: it bills the base fee).
"""

from unittest.mock import MagicMock

from agentpit.api.deps import get_db_session, get_onchain_admin, get_settings
from agentpit.onchain.tx_sender import TRANSFER_GAS
from agentpit.services.agent_accounts import AgentAccounts
from tests.onchain import _helpers as h


def _record_top_ups(monkeypatch, client) -> MagicMock:
    """A spy on every native transfer the app's admin is asked for, still sent for real."""
    admin = client.app.dependency_overrides[get_onchain_admin]()  # type: ignore[attr-defined]
    spy = MagicMock(wraps=admin.fund_gas)
    monkeypatch.setattr(admin, "fund_gas", spy)
    return spy


def _assert_one_exact_top_up(client, address, api_key, top_ups) -> None:
    overrides = client.app.dependency_overrides  # type: ignore[attr-defined]
    admin, settings = overrides[get_onchain_admin](), overrides[get_settings]()

    mine = [c.args[1] for c in top_ups.call_args_list if c.args[0].lower() == address.lower()]
    # One transfer, under the sponsor's AGENTPIT_MAX_TOPUP_GAS ceiling at today's
    # price (the old 0.02 native grant is twenty times that at anvil's ~1 gwei).
    assert len(mine) == 1, mine
    need = mine[0]
    assert 0 < need <= settings.max_topup_gas * admin.gas_price()
    # The wallet started empty, so the top-up was the whole need; the approvals
    # spent part of it and nothing was added afterwards.
    assert admin.native_balance(address) <= need
    # Exactly the three approvals went out from the wallet, and they are set.
    assert admin.transaction_count(address) == 3
    h.assert_approvals_set(admin, address)
    # The top-up and the approvals are on the account's daily row (measured
    # 46,487 / 46,487 / 45,996 gas for the approvals on anvil).
    booked = h.sponsored_gas(overrides[get_db_session](), api_key)
    assert TRANSFER_GAS + 3 * 21_000 < booked < TRANSFER_GAS + 3 * 60_000


def test_a_new_account_onboards_on_one_exact_top_up_and_no_grant(monkeypatch):
    client = h.fresh_client()
    top_ups = _record_top_ups(monkeypatch, client)

    body = h.register(client)
    user = body["user"]

    assert user["onboarded_at"] is not None
    _assert_one_exact_top_up(client, user["eth_address"], body["api_key"], top_ups)


def test_a_new_api_agent_onboards_the_same_way(monkeypatch):
    client = h.fresh_client()
    top_ups = _record_top_ups(monkeypatch, client)
    db = client.app.dependency_overrides[get_db_session]()  # type: ignore[attr-defined]

    onboard = h._auth_service(client)._onboard_new_account
    agent = AgentAccounts(db, onboard).create_api_agent("user_onboarding_gas")

    assert agent.onboarded_at is not None
    _assert_one_exact_top_up(client, agent.eth_address, agent.api_key, top_ups)
