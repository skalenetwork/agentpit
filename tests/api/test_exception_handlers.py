"""I4: an out-of-gas claim must surface as a domain error, not a bare 500.

`PositionService.redeem` raises `InsufficientGasError` when a user's wallet
can't cover a transaction's gas (see tests/onchain/test_auto_redeem.py for
the end-to-end proof against a real drained wallet). This is the narrower,
anvil-free proof that the exception itself maps to a structured 402 rather
than falling through to FastAPI's default unhandled-exception 500 -- built
against a throwaway app so it doesn't need the full stack or a live chain.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentpit.api.exception_handlers import register_exception_handlers
from agentpit.domain.exceptions import (
    AdminGasPausedError,
    BusinessRuleError,
    GasBudgetExceededError,
    InsufficientGasError,
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
    client = TestClient(_gas_stub_app(), raise_server_exceptions=False)
    r = client.get("/paused")
    assert r.status_code == 503
    assert "paused" in r.json()["detail"]


def test_gas_budget_is_429_with_retry_after():
    client = TestClient(_gas_stub_app(), raise_server_exceptions=False)
    r = client.get("/budget")
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "123"
    assert "00:00 UTC" in r.json()["detail"]
