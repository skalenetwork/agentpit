# tests/liquidity/test_house_provisioning.py
# --- the simulated-chain gate -----------------------------------------------
# Re-onboarding on a zero nonce repairs an account a disposable chain forgot.
# On a durable chain the same condition cannot be a wipe, and re-funding the
# house from the admin on every start would be a drain on the admin wallet.

PRICE = 1_000                         # wei per gas on the fake chain
ESTIMATE = 50_000                     # gas an approval is estimated at
# What three approvals need: each estimate padded by 20%, at the price.
APPROVALS_NEED = 3 * (ESTIMATE * 120 // 100) * PRICE


class _ApprovalCosts:
    """What `HouseAccountProvisioner._fund` reads to size the house's gas."""

    def gas_price(self):
        return PRICE

    def approval_calls(self):
        return ["approve_exchange", "approve_ctf", "approve_all"]

    def estimate_user_gas(self, fn, address):
        return ESTIMATE


class _FakeOnchain(_ApprovalCosts):
    chain_id = 31337                  # anvil: the one chain the gate honours

    def __init__(self, nonce: int = 0):
        self.funded = []
        self.nonce = nonce

    def transaction_count(self, address):
        return self.nonce             # 0 looks exactly like a chain wipe

    def faucet_drip(self, address, *, timeout=30):
        self.funded.append(("drip", address))

    def mint_to(self, address, amount_raw, *, timeout=30):
        self.funded.append(("mint", address, amount_raw))

    def fund_gas(self, address, value_wei, *, timeout=30):
        self.funded.append(("gas", address, value_wei))

    def grant_user_approvals(self, account, *, timeout=30):
        self.funded.append(("approvals", account))


def _provisioner(simulated: bool, nonce: int = 0):
    from agentpit.config import Settings
    from agentpit.liquidity.house_accounts import HouseAccountProvisioner
    onchain = _FakeOnchain(nonce)
    settings = Settings(AGENTPIT_SIMULATED_CHAIN=simulated)
    return HouseAccountProvisioner(None, onchain, settings), onchain


class _Key:
    address = "0x" + "11" * 20


class _User:
    user_id = "u1"
    email = "house-bot-0@agentpit.local"
    eth_address = "0x" + "11" * 20
    eth_key = _Key()


def test_zero_nonce_reonboards_on_a_simulated_chain():
    prov, onchain = _provisioner(True)
    prov._maybe_reonboard(_User())
    assert onchain.funded, "a wiped chain must be repaired"


def test_a_house_that_sent_its_approvals_is_not_reonboarded():
    prov, onchain = _provisioner(True, nonce=3)
    prov._maybe_reonboard(_User())
    assert onchain.funded == [], "exact funding leaves the balance near zero, not the nonce"


def test_zero_nonce_does_not_regrant_on_a_durable_chain():
    prov, onchain = _provisioner(False)
    prov._maybe_reonboard(_User())
    assert onchain.funded == [], "login must not fund the house again"


def test_zero_nonce_does_not_regrant_on_a_durable_chain_even_if_simulated():
    prov, onchain = _provisioner(True)
    onchain.chain_id = 324705682
    prov._maybe_reonboard(_User())
    assert onchain.funded == []


def test_house_is_funded_by_one_mint_not_repeated_drips():
    """One mint of a stated size — not N repetitions of a user's grant.

    The faucet's drip amount is the USER grant now ($100k). Funding the house
    that way would need ten trillion transactions, which is the whole reason
    mintTo exists.
    """
    from agentpit.config import Settings
    from agentpit.liquidity.house_accounts import HouseAccountProvisioner

    onchain = _FakeOnchain()
    settings = Settings()
    HouseAccountProvisioner(None, onchain, settings)._fund(_Key())

    mints = [c for c in onchain.funded if c[0] == "mint"]
    assert mints == [("mint", _Key.address, settings.house_mint_raw)]
    assert not [c for c in onchain.funded if c[0] == "drip"]


def test_a_new_house_account_gets_exactly_the_gas_of_its_three_approvals():
    """The house sends nothing after its approvals (fills are the admin's
    matchOrders), so its gas is the three approvals' estimates plus 20%, at
    the current price."""
    from agentpit.config import Settings
    from agentpit.liquidity.house_accounts import HouseAccountProvisioner

    onchain = _FakeOnchain()
    HouseAccountProvisioner(None, onchain, Settings())._fund(_Key())

    assert [c for c in onchain.funded if c[0] == "gas"] == [
        ("gas", _Key.address, APPROVALS_NEED)
    ]
