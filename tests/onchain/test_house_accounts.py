from web3 import Web3

from agentpit.config import Settings
from agentpit.liquidity.house_accounts import HouseAccountProvisioner
from agentpit.onchain.admin import OnchainAdmin
from agentpit.onchain.contracts import Contracts
from agentpit.onchain.deployment import Deployment
from agentpit.onchain.web3_client import Web3Client
from tests.db_helpers import fresh_test_db
from tests.onchain._helpers import HOUSE_TEST_GAS_FLOOR_WEI


def _provisioner(count=3, floor=HOUSE_TEST_GAS_FLOOR_WEI):
    s = Settings(
        liquidity_house_account_count=count,
        liquidity_gas_floor_wei=floor,
    )
    d = Deployment.load(s.deployment_path)
    w = Web3Client(s, d)
    admin = OnchainAdmin(w, Contracts(w.web3, d))
    return HouseAccountProvisioner(fresh_test_db(), admin, s), admin, d


def test_provision_creates_and_funds():
    prov, admin, _d = _provisioner(count=3)
    users = prov.ensure_provisioned()
    assert len(users) == 3
    for u in users:
        assert u.is_bot is True
        assert u.onboarded_at is not None
        # The house is funded by one mintTo of house_mint_raw, not by drips.
        # Asserting against signup_grant_raw would be vacuous now: 1e24 clears
        # a $100k grant whether the house was minted, dripped, or funded by
        # accident, so the one test proving house funding would prove nothing.
        assert admin.usd_balance(u.eth_address) >= prov._settings.house_mint_raw
        # Gas: funded AT the floor, then the three approvals were paid out of
        # it. They cost far less than 10**15 at anvil's near-zero base fee, so
        # a lower balance means the account was funded with some other amount.
        native = admin.native_balance(u.eth_address)
        assert HOUSE_TEST_GAS_FLOOR_WEI - 10**15 < native <= HOUSE_TEST_GAS_FLOOR_WEI


def test_provision_is_idempotent():
    prov, _admin, _d = _provisioner(count=3)
    first = prov.ensure_provisioned()
    second = prov.ensure_provisioned()
    assert {u.email for u in first} == {u.email for u in second}
    assert len(second) == 3  # no duplicates created


def test_a_zero_gas_floor_still_funds_the_house_for_its_approvals():
    """A floor of 0 switches the refill loop off, and says nothing about what
    a NEW house account needs: it still signs three approvals before it can
    trade. It is funded with what they cost, so provisioning completes."""
    prov, admin, d = _provisioner(count=1, floor=0)
    (user,) = prov.ensure_provisioned()
    assert user.onboarded_at is not None
    # The approvals were mined: the exchange may move the house's collateral.
    allowance = admin._contracts.usd.functions.allowance(  # noqa: SLF001
        Web3.to_checksum_address(user.eth_address), d.exchange
    ).call()
    assert allowance > 0
    # Funded with the approvals' need, not a floor-sized stash: what is left
    # is the unused part of the pad, a few thousand times less than the test
    # floor.
    assert 0 < admin.native_balance(user.eth_address) < HOUSE_TEST_GAS_FLOOR_WEI
