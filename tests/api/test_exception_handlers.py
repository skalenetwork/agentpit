"""I4: an out-of-gas claim must surface as a domain error, not a bare 500.

`PositionService.redeem` raises `InsufficientGasError` when a user's wallet
can't cover a transaction's gas (see tests/onchain/test_auto_redeem.py for
the end-to-end proof against a real drained wallet). This is the narrower,
anvil-free proof that the exception itself maps to a structured 402 rather
than falling through to FastAPI's default unhandled-exception 500 -- built
against a throwaway app so it doesn't need the full stack or a live chain.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentpit.api.exception_handlers import register_exception_handlers
from agentpit.domain.exceptions import (
    AdminGasPausedError,
    BusinessRuleError,
    GasBudgetExceededError,
    InsufficientGasError,
    NothingToClaimError,
    TransactionInProgressError,
    TransactionRevertedError,
)


def _stub_app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/boom-gas")
    def _boom_gas():
        raise InsufficientGasError(
            "wallet balance too low to pay for this transaction's gas"
        )

    @app.get("/boom-business-rule")
    def _boom_business_rule():
        raise BusinessRuleError("something else entirely")

    return app


def test_insufficient_gas_is_a_structured_402_not_a_500():
    client = TestClient(_stub_app(), raise_server_exceptions=False)
    resp = client.get("/boom-gas")
    assert resp.status_code == 402
    assert "gas" in resp.json()["detail"]


def test_plain_business_rule_errors_still_map_to_400():
    """`InsufficientGasError` is a `BusinessRuleError` subclass -- confirm
    adding its own handler didn't change what the generic catch-all does for
    every other business-rule failure."""
    client = TestClient(_stub_app(), raise_server_exceptions=False)
    resp = client.get("/boom-business-rule")
    assert resp.status_code == 400
    assert resp.json()["detail"] == "something else entirely"


def _gas_stub_app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/paused")
    def _paused():
        raise AdminGasPausedError()

    @app.get("/budget")
    def _budget():
        raise GasBudgetExceededError(retry_after=123)

    return app


def test_admin_gas_paused_is_503():
    """The breaker now also refuses gas top-ups for claims, splits, merges and
    onboarding, so the wording no longer says it is trading that paused."""
    client = TestClient(_gas_stub_app(), raise_server_exceptions=False)
    r = client.get("/paused")
    assert r.status_code == 503
    assert r.json()["detail"] == (
        "the platform's gas wallet is running low — try again later"
    )


def test_gas_budget_is_429_with_retry_after():
    """Split and merge spend the same daily budget as fills, so the wording
    says gas, not trading gas."""
    client = TestClient(_gas_stub_app(), raise_server_exceptions=False)
    r = client.get("/budget")
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "123"
    assert r.json()["detail"] == (
        "this account has used its daily gas budget — it resets at 00:00 UTC"
    )


def _sponsor_stub_app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/in-progress")
    def _in_progress():
        raise TransactionInProgressError()

    @app.get("/nothing")
    def _nothing():
        raise NothingToClaimError()

    @app.get("/reverted")
    def _reverted():
        raise TransactionRevertedError("the claim transaction reverted on chain")

    return app


def test_a_held_transaction_lock_is_409_logged_at_info(caplog):
    """A second claim, split or merge while one is still being sent for the
    same account. Not a `BusinessRuleError` (400): nothing in the request is
    wrong, and it may simply be retried. INFO: the lock refusing is the lock
    working."""
    caplog.set_level(logging.INFO, logger="agentpit.api.exception_handlers")
    client = TestClient(_sponsor_stub_app(), raise_server_exceptions=False)
    r = client.get("/in-progress")
    assert r.status_code == 409
    assert r.json() == {
        "detail": "another transaction for this account is in progress — try again in a moment"
    }
    ours = [rec for rec in caplog.records if rec.name == "agentpit.api.exception_handlers"]
    assert [rec.levelno for rec in ours] == [logging.INFO]
    assert not issubclass(TransactionInProgressError, BusinessRuleError)


def test_nothing_to_claim_and_a_reverted_transaction_are_400():
    client = TestClient(_sponsor_stub_app(), raise_server_exceptions=False)
    r = client.get("/nothing")
    assert (r.status_code, r.json()["detail"]) == (400, "nothing to claim")
    r = client.get("/reverted")
    assert (r.status_code, r.json()["detail"]) == (
        400,
        "the claim transaction reverted on chain",
    )
