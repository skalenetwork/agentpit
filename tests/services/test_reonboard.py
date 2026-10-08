"""`_maybe_reonboard` and the accounts whose key is already out.

Wallets are custodial and nobody can export a key any more, but some accounts
did before the routes were removed, and KEY_EXPORTED_AT still marks them. For
those a zero balance can mean the holder emptied the wallet on purpose, so the
chain-wipe repair must never re-fund them.

The pair below is what makes the lock test mean something: the same wiped,
onboarded row IS re-funded when nothing was exported, so the second test fails
if the lock goes away -- not just if the chain is unreachable.
"""

from agentpit.auth.jwt import JwtCoder
from agentpit.config import Settings
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.services.auth_service import AuthService


class _WipedChain:
    """Every wallet reads empty, as after a chain reset; records what ran."""

    deployment_id = "test-deployment"

    def __init__(self):
        self.calls: list[str] = []

    def native_balance(self, *_args, **_kwargs):
        self.calls.append("native_balance")
        return 0

    def usd_balance(self, *_args, **_kwargs):
        self.calls.append("usd_balance")
        return 0

    def fund_gas(self, *_args, **_kwargs):
        self.calls.append("fund_gas")

    def faucet_drip(self, *_args, **_kwargs):
        self.calls.append("faucet_drip")

    def grant_user_approvals(self, *_args, **_kwargs):
        self.calls.append("grant_user_approvals")


def _reonboard(email: str, *, exported_at: int | None) -> list[str]:
    settings = Settings().model_copy(update={"simulated_chain": True})
    db = DbSession(settings.database_url)
    chain = _WipedChain()
    service = AuthService(db, JwtCoder(settings), chain, settings)
    try:
        with db.write() as conn:
            user_id, _acct, _key = TableWrite.create_user(
                conn, email=email, password_hash=None, handle=None
            )
            TableWrite.mark_user_onboarded(conn, user_id)
            if exported_at is not None:
                # Nothing writes this column any more; a pre-removal export
                # is the only way a row has it.
                conn.execute(
                    "UPDATE users SET KEY_EXPORTED_AT = %s WHERE USER_ID = %s",
                    (exported_at, user_id),
                )
        with db.read() as conn:
            user = TableRead.get_user_by_userid(conn, user_id)
        service._maybe_reonboard(user)
    finally:
        db.close()
    return chain.calls


def test_a_wiped_wallet_only_we_hold_is_refunded():
    assert "fund_gas" in _reonboard("custodial@example.com", exported_at=None)


def test_a_wallet_whose_key_was_exported_is_never_refunded():
    assert _reonboard("exported@example.com", exported_at=1_700_000_000) == []
