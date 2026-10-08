"""`_maybe_reonboard`: what reads as a wiped chain, and who never gets the repair.

The signal is the wallet's nonce. Onboarding sends three approvals from the
wallet, so an onboarded account the chain has never seen send from it is one
the chain forgot. The native balance was the signal until exact top-ups made
a near-empty wallet the normal state after every action.

Wallets are custodial and nobody can export a key any more, but some accounts
did before the routes were removed, and KEY_EXPORTED_AT still marks them.
Those never get the repair, whatever their wallet shows.

The pairing is what makes each refusal mean something: the same onboarded row
on the same wiped chain IS re-onboarded when nothing was exported and the
nonce is zero, so a refusal test fails if its gate goes away -- not just if
the chain is unreachable.
"""

import logging
from contextlib import nullcontext

from agentpit.auth.jwt import JwtCoder
from agentpit.config import Settings
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.services.auth_service import AuthService
from agentpit.services.gas_sponsor import UserGasSponsor
from tests.onboarding_fakes import OnboardingChain


class _WipedChain(OnboardingChain):
    """Every wallet reads empty, as after a chain reset -- or, with a nonce
    above zero, as after an ordinary day of exact top-ups."""

    deployment_id = "test-deployment"

    def native_balance(self, *_args, **_kwargs):
        self.calls.append("native_balance")
        return 0

    def usd_balance(self, *_args, **_kwargs):
        self.calls.append("usd_balance")
        return 0


def _reonboard(
    email: str,
    *,
    exported_at: int | None = None,
    nonce: int = 0,
    hold_lock: bool = False,
) -> list[str]:
    settings = Settings().model_copy(update={"simulated_chain": True})
    db = DbSession(settings.database_url)
    chain = _WipedChain(nonce=nonce)
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
        assert user is not None
        # Another request mid-send for this account, when asked for.
        held = (
            UserGasSponsor(db, chain, settings).locked(user)  # type: ignore[arg-type]
            if hold_lock
            else nullcontext()
        )
        with held:
            service._maybe_reonboard(user)
    finally:
        db.close()
    return chain.calls


def test_a_wiped_wallet_only_we_hold_is_reonboarded():
    calls = _reonboard("custodial@example.com")
    assert "fund_gas" in calls and calls.count("send_as_user") == 3


def test_a_wallet_that_has_sent_is_left_alone_however_empty():
    # The nonce is read and nothing else: no drip, no top-up, no approvals.
    assert _reonboard("spent@example.com", nonce=3) == ["transaction_count"]


def test_a_wallet_whose_key_was_exported_is_never_reonboarded():
    assert _reonboard("exported@example.com", exported_at=1_700_000_000) == []


def test_a_transaction_in_progress_skips_the_repair_without_a_traceback(caplog):
    with caplog.at_level(logging.INFO, logger="agentpit.services.auth_service"):
        calls = _reonboard("busy@example.com", hold_lock=True)
    assert calls == ["transaction_count"]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
