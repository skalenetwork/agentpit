"""Just enough `OnchainAdmin` for `AuthService` onboarding.

Onboarding used to be three admin calls -- fund_gas, faucet_drip,
grant_user_approvals -- and every fake of it spelled out those three. The
approvals now go through `UserGasSponsor`, which prices them, tops the wallet
up to exactly that and sends them signed by the user's key, so a fake needs
the surface the sponsor reads as well. One copy of it lives here; each test
file subclasses it for the one thing it is about (a paused breaker, a failing
top-up, a wiped chain, a chain that must not be touched).
"""

from web3.datastructures import AttributeDict

#: What every fake approval estimates at, and mines at: near the real ones
#: (approve 46,487, setApprovalForAll 45,996 on anvil, 2026-10-08).
APPROVAL_GAS = 46_000
#: About anvil's eth_gasPrice (a 1 gwei tip over a 7 wei base fee).
GAS_PRICE = 1_000_000_000
#: The limit the sponsor sends each approval with: the estimate plus 20%.
APPROVAL_LIMIT = APPROVAL_GAS * 120 // 100
#: Three approvals at their limits and `GAS_PRICE`: an empty wallet's whole top-up.
ONBOARDING_NEED = 3 * APPROVAL_LIMIT * GAS_PRICE


class OnboardingChain:
    """Records, in order, what onboarding did (`calls`), the top-ups it sent
    (`funded`, as (address, wei)) and each user-signed send (`sent`, as
    (signer address, call, gas limit, max fee))."""

    deployment_id = "0xctf"
    # anvil: the re-onboarding repair only runs on a chain that can be wiped.
    chain_id = 31337

    def __init__(self, *, nonce: int = 0) -> None:
        self.calls: list[str] = []
        self.funded: list[tuple[str, int]] = []
        self.sent: list[tuple[str, object, int, int]] = []
        self._nonce = nonce

    # --- read and sent by AuthService itself --------------------------

    def faucet_drip(self, recipient, *, timeout=30):
        self.calls.append("faucet_drip")
        return AttributeDict({"status": 1, "gasUsed": 50_000})

    def approval_calls(self):
        # Stand-ins: the sponsor only hands them back to `estimate_user_gas`
        # and `send_as_user`, both fakes here.
        return ["approve(exchange)", "approve(ctf)", "setApprovalForAll(exchange)"]

    def transaction_count(self, address):
        self.calls.append("transaction_count")
        return self._nonce

    def usd_balance(self, address):
        return 100

    # --- read and sent by UserGasSponsor --------------------------------

    def gas_price(self):
        return GAS_PRICE

    def estimate_user_gas(self, fn, address):
        return APPROVAL_GAS

    def native_balance(self, address):
        return 0

    def fund_gas(self, user_address, value_wei, *, timeout=30):
        self.calls.append("fund_gas")
        self.funded.append((user_address, value_wei))
        return AttributeDict({"status": 1, "gasUsed": 21_000})

    def send_as_user(self, user_account, fn, *, gas, max_fee, timeout=30):
        self.calls.append("send_as_user")
        self.sent.append((user_account.address, fn, gas, max_fee))
        return AttributeDict({"status": 1, "gasUsed": APPROVAL_GAS})
