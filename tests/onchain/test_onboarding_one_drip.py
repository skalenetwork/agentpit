"""One account, one collateral grant, on the real faucet (fake-chain matrix: tests/services/
test_onboarding_one_drip.py): `Faucet.drip` mints with no per-address guard, so a retry after
an onboarding that failed once the drip landed (its top-up timed out) must hold one grant.
"""

import pytest
from web3.exceptions import TimeExhausted

from agentpit.api.deps import get_db_session, get_onchain_admin
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import GasTopUpTimeoutError, OnboardingError
from tests.onchain._helpers import _auth_service, fresh_client, unique_email


def _world():
    client = fresh_client()
    overrides = client.app.dependency_overrides  # type: ignore[attr-defined]
    return _auth_service(client), overrides[get_db_session](), overrides[get_onchain_admin]()


def test_the_deployment_grant_is_what_the_faucet_drips():
    """The drip guard's `signup_grant_raw` (deployment file) must equal the faucet's amount."""
    _, _, admin = _world()

    assert admin.signup_grant_raw == admin._contracts.faucet.functions.amount().call()  # noqa: SLF001


def test_a_retry_after_a_failed_onboarding_leaves_exactly_one_grant(monkeypatch):
    service, db, admin = _world()
    grant = admin._contracts.faucet.functions.amount().call()  # noqa: SLF001
    with db.write() as conn:
        user_id, acct, _key = TableWrite.create_user(
            conn, email=unique_email(), password_hash=None, handle=None
        )

    real_fund_gas, failed = admin.fund_gas, []

    def fund_gas_once_late(user_address, value_wei, *, timeout=30):
        if not failed:
            failed.append(True)
            raise TimeExhausted("no receipt for the top-up")
        return real_fund_gas(user_address, value_wei, timeout=timeout)

    monkeypatch.setattr(admin, "fund_gas", fund_gas_once_late)

    # Which error the caller sees is the unit tests' business; this one is
    # about what the chain holds afterwards.
    with pytest.raises((GasTopUpTimeoutError, OnboardingError)):
        service._onboard_new_account(user_id, acct)
    assert admin.usd_balance(acct.address) == grant  # the drip landed before the top-up failed
    assert admin.transaction_count(acct.address) == 0

    user = service._onboard_new_account(user_id, acct)

    assert user.onboarded_at is not None
    assert admin.usd_balance(acct.address) == grant
    assert admin.transaction_count(acct.address) == 3
    with db.read() as conn:
        assert TableRead.get_total_deposited(conn, user_id, 0) == grant
