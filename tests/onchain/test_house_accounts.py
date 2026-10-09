from agentpit.config import Settings
from agentpit.liquidity.house_accounts import HouseAccountProvisioner
from agentpit.onchain.admin import OnchainAdmin
from agentpit.onchain.contracts import Contracts
from agentpit.onchain.deployment import Deployment
from agentpit.onchain.web3_client import Web3Client
from tests.db_helpers import fresh_test_db
from tests.onchain._helpers import assert_approvals_set


def _provisioner():
    s = Settings()
    d = Deployment.load(s.deployment_path)
    w = Web3Client(s, d)
    admin = OnchainAdmin(w, Contracts(w.web3, d))
    return HouseAccountProvisioner(fresh_test_db(), admin, s), admin


def test_provision_creates_and_funds():
    prov, admin = _provisioner()
    u = prov.ensure_provisioned()
    assert u.is_bot is True
    assert u.onboarded_at is not None
    # The house is funded by one mintTo of house_mint_raw, not by drips.
    # Asserting against signup_grant_raw would be vacuous now: 1e24 clears
    # a $100k grant whether the house was minted, dripped, or funded by
    # accident, so the one test proving house funding would prove nothing.
    assert admin.usd_balance(u.eth_address) >= prov._settings.house_mint_raw
    assert admin.native_balance(u.eth_address) > 0
    assert_approvals_set(admin, u.eth_address)


def test_provision_is_idempotent():
    prov, _admin = _provisioner()
    assert prov.ensure_provisioned().user_id == prov.ensure_provisioned().user_id
