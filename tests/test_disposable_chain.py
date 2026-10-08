import logging

from agentpit.api.app import _warn_if_simulated_on_durable_chain
from agentpit.auth.jwt import JwtCoder
from agentpit.config import Settings
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.onchain.deployment import is_disposable_chain
from agentpit.services.auth_service import AuthService
from tests.db_helpers import fresh_test_db

SKALE_BASE_TESTNET = 324705682


def test_only_anvil_is_disposable():
    assert is_disposable_chain(31337)
    assert not is_disposable_chain(SKALE_BASE_TESTNET)
    assert not is_disposable_chain(1187947933)


class _EmptyWallets:
    def __init__(self, chain_id):
        self.chain_id = chain_id
        self.funded = []

    def native_balance(self, _a):
        return 0

    def fund_gas(self, address, _wei, *, timeout=30):
        self.funded.append(address)

    def faucet_drip(self, *_a, **_k):
        pass

    def grant_user_approvals(self, *_a, **_k):
        pass

    def usd_balance(self, _a):
        return 0

    deployment_id = "0xctf"


def _reonboard(chain_id):
    db = fresh_test_db()
    settings = Settings(AGENTPIT_SIMULATED_CHAIN=True)
    chain = _EmptyWallets(chain_id)
    service = AuthService(db, JwtCoder(settings), chain, settings)  # type: ignore[arg-type]
    with db.write() as conn:
        user_id, _a, _k = TableWrite.create_user(conn, email=f"r{chain_id}@example.com", password_hash=None, handle=None)
        TableWrite.mark_user_onboarded(conn, user_id)
        user = TableRead.get_user_by_userid(conn, user_id)
    service._maybe_reonboard(user)
    return chain


def test_login_regrants_on_anvil():
    assert len(_reonboard(31337).funded) == 1


def test_login_never_regrants_on_a_durable_chain_even_if_simulated_is_true():
    assert _reonboard(SKALE_BASE_TESTNET).funded == []


def test_startup_shouts_when_simulated_is_set_on_a_durable_chain(caplog):
    with caplog.at_level(logging.ERROR, logger="agentpit.api.app"):
        _warn_if_simulated_on_durable_chain(Settings(AGENTPIT_SIMULATED_CHAIN=True), SKALE_BASE_TESTNET)
        _warn_if_simulated_on_durable_chain(Settings(AGENTPIT_SIMULATED_CHAIN=True), 31337)
        _warn_if_simulated_on_durable_chain(Settings(AGENTPIT_SIMULATED_CHAIN=False), SKALE_BASE_TESTNET)
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and "AGENTPIT_SIMULATED_CHAIN" in errors[0].getMessage()
