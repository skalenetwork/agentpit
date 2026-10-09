# tests/liquidity/test_house_provisioning.py
# --- the simulated-chain gate -----------------------------------------------
# Re-onboarding on a zero balance repairs an account a disposable chain forgot.
# On a durable chain the same condition means the account spent its gas, and
# re-granting on login would be a faucet anyone could drain on repeat.

class _FakeOnchain:
    def __init__(self):
        self.funded = []

    def native_balance(self, address):
        return 0                      # looks exactly like a chain wipe

    def faucet_drip(self, address, *, timeout=30):
        self.funded.append(("drip", address))

    def mint_to(self, address, amount_raw, *, timeout=30):
        self.funded.append(("mint", address, amount_raw))

    def fund_gas(self, address, value_wei, *, timeout=30):
        self.funded.append(("gas", address))

    def grant_user_approvals(self, account, *, timeout=30):
        self.funded.append(("approvals", account))


def _provisioner(simulated: bool):
    from agentpit.config import Settings
    from agentpit.liquidity.house_accounts import HouseAccountProvisioner
    onchain = _FakeOnchain()
    settings = Settings(AGENTPIT_SIMULATED_CHAIN=simulated)
    return HouseAccountProvisioner(None, onchain, settings), onchain


class _Key:
    address = "0x" + "11" * 20


class _User:
    user_id = "u1"
    email = "house-bot-0@agentpit.local"
    eth_address = "0x" + "11" * 20
    eth_key = _Key()


def test_zero_balance_reonboards_on_a_simulated_chain():
    prov, onchain = _provisioner(True)
    prov._maybe_reonboard(_User())
    assert onchain.funded, "a wiped chain must be repaired"


def test_zero_balance_does_not_regrant_on_a_durable_chain():
    prov, onchain = _provisioner(False)
    prov._maybe_reonboard(_User())
    assert onchain.funded == [], "login must not mint a fresh gas grant"


def test_house_is_funded_by_one_mint_not_repeated_drips():
    """One mint of a stated size — not N repetitions of a user's grant.

    The faucet's drip amount is the USER grant now ($100k). Funding the house
    that way would need ten trillion transactions, which is the whole reason
    mintTo exists.
    """
    from agentpit.config import Settings
    from agentpit.liquidity.house_accounts import HouseAccountProvisioner

    calls = []

    class _Onchain:
        def faucet_drip(self, address, *, timeout=30):
            calls.append(("drip", address))

        def mint_to(self, address, amount_raw, *, timeout=30):
            calls.append(("mint", address, amount_raw))

        def fund_gas(self, address, value_wei, *, timeout=30):
            calls.append(("gas", address))

        def grant_user_approvals(self, account, *, timeout=30):
            calls.append(("approvals",))

    class _Key:
        address = "0x" + "11" * 20

    settings = Settings()
    prov = HouseAccountProvisioner(None, _Onchain(), settings)
    prov._fund(_Key())

    mints = [c for c in calls if c[0] == "mint"]
    assert len(mints) == 1
    assert mints[0][2] == settings.house_mint_raw
    assert not [c for c in calls if c[0] == "drip"]
