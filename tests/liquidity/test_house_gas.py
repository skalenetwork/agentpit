# tests/liquidity/test_house_gas.py
import pytest

from agentpit.liquidity.house_accounts import gas_topup_wei

ETH = 10**18
FLOOR = 5 * ETH
TARGET = 100 * ETH


def test_no_topup_while_above_the_floor():
    assert gas_topup_wei(6 * ETH, FLOOR, TARGET) == 0
    assert gas_topup_wei(TARGET, FLOOR, TARGET) == 0


def test_floor_itself_is_not_a_refill():
    assert gas_topup_wei(FLOOR, FLOOR, TARGET) == 0


def test_below_the_floor_refills_to_target():
    assert gas_topup_wei(4 * ETH, FLOOR, TARGET) == 96 * ETH


def test_production_dust_is_refilled():
    # The balance the mirror actually stalled on: starved, but not zero, so the
    # old "refill at exactly zero" rule never fired.
    dust = 11_211_539_964_876
    assert gas_topup_wei(dust, FLOOR, TARGET) == TARGET - dust


def test_zero_floor_disables_topups():
    assert gas_topup_wei(0, 0, TARGET) == 0


def test_never_returns_negative_when_misconfigured():
    # floor above target: a balance under the floor but over the target must
    # not ask for a negative transfer.
    assert gas_topup_wei(50 * ETH, 100 * ETH, 10 * ETH) == 0


# --- the simulated-chain gate -----------------------------------------------
# Re-onboarding on a zero balance repairs an account a disposable chain forgot.
# On a durable chain the same condition means the account spent its gas, and
# re-funding it from the admin every time it ran dry would be a drain on the
# admin wallet.

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

    def __init__(self):
        self.funded = []

    def native_balance(self, address):
        return 0                      # looks exactly like a chain wipe

    def faucet_drip(self, address, *, timeout=30):
        self.funded.append(("drip", address))

    def mint_to(self, address, amount_raw, *, timeout=30):
        self.funded.append(("mint", address, amount_raw))

    def fund_gas(self, address, value_wei, *, timeout=30):
        self.funded.append(("gas", address, value_wei))

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
    assert onchain.funded == [], "login must not fund a house that ran dry"


def test_zero_balance_does_not_regrant_on_a_durable_chain_even_if_simulated():
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

    calls = []

    class _Onchain(_ApprovalCosts):
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


def test_a_new_house_account_is_funded_at_the_gas_floor():
    """Gas for a new house account is the floor, not a user's signup grant.

    Users get no grant any more: every transaction they sign is topped up to
    exactly its need (`UserGasSponsor`). The house signs its own mirror splits
    and needs a standing balance. The floor is enough to start on, and
    `top_up_gas` lifts it to the target within one check interval.
    """
    from agentpit.config import Settings
    from agentpit.liquidity.house_accounts import HouseAccountProvisioner

    onchain = _FakeOnchain()
    settings = Settings()
    HouseAccountProvisioner(None, onchain, settings)._fund(_Key())  # noqa: SLF001

    assert [c for c in onchain.funded if c[0] == "gas"] == [
        ("gas", _Key.address, settings.liquidity_gas_floor_wei)
    ]


@pytest.mark.parametrize("floor", [0, 1, APPROVALS_NEED - 1])
def test_a_new_house_account_can_always_pay_for_its_three_approvals(floor):
    """The floor is what the account is held at later, not what it needs now.
    A floor of 0 (which also switches the refill loop off) or a tiny one would
    leave the house unable to pay for the approvals it signs next, and
    `ensure_provisioned` would fail startup. So a new account is funded with
    whichever is more: the floor, or the gas of the three approvals (each
    estimate plus 20%, at the current price)."""
    from agentpit.config import Settings
    from agentpit.liquidity.house_accounts import HouseAccountProvisioner

    onchain = _FakeOnchain()
    settings = Settings(AGENTPIT_LIQUIDITY_GAS_FLOOR_WEI=floor)
    HouseAccountProvisioner(None, onchain, settings)._fund(_Key())  # noqa: SLF001

    assert [c for c in onchain.funded if c[0] == "gas"] == [
        ("gas", _Key.address, APPROVALS_NEED)
    ]


def test_a_floor_above_the_approvals_need_is_still_what_a_new_house_gets():
    from agentpit.config import Settings
    from agentpit.liquidity.house_accounts import HouseAccountProvisioner

    onchain = _FakeOnchain()
    settings = Settings(AGENTPIT_LIQUIDITY_GAS_FLOOR_WEI=APPROVALS_NEED + 1)
    HouseAccountProvisioner(None, onchain, settings)._fund(_Key())  # noqa: SLF001

    assert [c for c in onchain.funded if c[0] == "gas"] == [
        ("gas", _Key.address, APPROVALS_NEED + 1)
    ]


# --- the admin gas breaker ---------------------------------------------------
# A paused breaker refuses every sponsored send, `fund_gas` included, until the
# admin is refilled. The loop retries every few minutes, so each account would
# otherwise log a traceback per cycle for something already logged as an ERROR
# by the balance loop.

def test_top_up_while_the_breaker_is_paused_is_quiet(caplog):
    import logging

    from agentpit.config import Settings
    from agentpit.domain.exceptions import AdminGasPausedError
    from agentpit.liquidity.house_accounts import HouseAccountProvisioner

    attempts = []

    class _PausedOnchain:
        def native_balance(self, address):
            return 0                  # below the floor: a top-up is due

        def fund_gas(self, address, value_wei, *, timeout=30):
            attempts.append(address)
            raise AdminGasPausedError()

    prov = HouseAccountProvisioner(None, _PausedOnchain(), Settings())
    with caplog.at_level(logging.DEBUG, logger="agentpit.liquidity.house_accounts"):
        assert prov.top_up_gas([_User(), _User()]) == 0   # type: ignore[list-item]

    assert len(attempts) == 2, "one refused account must not stop the others being tried"
    assert not [r for r in caplog.records if r.exc_info], "no traceback per account"
    assert any("breaker is paused" in r.getMessage() for r in caplog.records)
