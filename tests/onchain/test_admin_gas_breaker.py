import secrets

import pytest
from web3 import Web3

from agentpit.api.deps import get_onchain_admin
from agentpit.domain.exceptions import AdminGasPausedError
from tests.db_helpers import fresh_test_db
from tests.onchain._helpers import create_market, fresh_client, hdr, register


def _trip(admin):
    """Pause the breaker. Returns (sender, the stop level it had before), so the
    caller can put it back: the app object is shared by every later test.

    Only sets the level: the caller refreshes the balance inside its own
    try/finally, so a refresh that raises still gets the level put back."""
    sender = admin._client.admin_sender  # noqa: SLF001
    original = sender._stop_gas          # noqa: SLF001
    sender._stop_gas = 10**30            # noqa: SLF001  any balance is below this
    return sender, original


def test_paused_breaker_refuses_fills_and_grants_but_not_the_oracle():
    client = fresh_client()
    maker, taker = register(client)["api_key"], register(client)["api_key"]
    market = create_market(client)
    client.post(f"/markets/{market['market_id']}/split_position", headers=hdr(maker), json={"amount": 20_000_000}).raise_for_status()
    yes = market["erc1155_tokens"][0][0]
    client.post("/order", headers=hdr(maker), json={"token_id": yes, "side": "SELL", "price": "0.5", "size": 10}).raise_for_status()

    admin = client.app.dependency_overrides[get_onchain_admin]()  # type: ignore[attr-defined]
    sender, original_stop_gas = _trip(admin)
    try:
        assert admin.refresh_admin_gas()[1] == "paused"       # reads the balance against the raised level
        r = client.post("/order", headers=hdr(taker), json={"token_id": yes, "side": "BUY", "price": "0.5", "size": 10})
        assert r.status_code == 503
        with fresh_test_db().read() as conn:
            assert conn.execute("SELECT COUNT(*) AS N FROM trades").fetchone()["N"] == 0
        stranger = Web3.to_checksum_address("0x" + secrets.token_hex(20))
        for call in (lambda: admin.fund_gas(stranger, 1), lambda: admin.faucet_drip(stranger), lambda: admin.mint_to(stranger, 1)):
            with pytest.raises(AdminGasPausedError):
                call()
        # essential: the oracle's own condition preparation still goes out
        receipt = admin.prepare_condition(admin.oracle_address, secrets.token_bytes(32), 2)
        assert receipt["status"] == 1
    finally:
        sender._stop_gas = original_stop_gas   # noqa: SLF001  not 0: that would leave the breaker off for every later test
