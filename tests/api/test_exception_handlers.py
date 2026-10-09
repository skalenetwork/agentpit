"""I4: an out-of-gas claim must surface as a domain error, not a bare 500.

`UserGasSponsor` raises `InsufficientGasError` when a user's wallet can't
cover a transaction's gas (see tests/onchain/test_sponsored_positions.py for
the end-to-end proof against a real drained wallet). This is the narrower,
anvil-free proof that the exception itself maps to a structured 402 rather
than falling through to FastAPI's default unhandled-exception 500 -- built
against a throwaway app so it doesn't need the full stack or a live chain.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentpit.api.exception_handlers import register_exception_handlers
from agentpit.domain import exceptions as errors
from agentpit.domain.exceptions import BusinessRuleError, InsufficientGasError


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


def _get(exc: Exception):
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/raises")
    def _raises():
        raise exc

    return TestClient(app, raise_server_exceptions=False).get("/raises")


_PAUSED = "the platform's gas wallet is running low — try again later"
_BUDGET = "this account has used its daily gas budget — it resets at 00:00 UTC"
_IN_PROGRESS = "another transaction for this account is in progress — try again in a moment"
_BUSY = "the platform is busy — try again in a moment"
_PENDING = (
    "the transaction was sent but is not confirmed yet — it will "
    "appear in your history once it lands; do not repeat it"
)
_PRICE_MOVED = "the network fee rose while sending — try again in a moment"
_REVERTED = "the claim transaction reverted on chain"
_RETRY = {"Retry-After": "123"}


@pytest.mark.parametrize(
    ("exc", "status", "detail", "headers"),
    [
        # The breaker also refuses top-ups for claims, splits, merges and onboarding.
        (errors.AdminGasPausedError(), 503, _PAUSED, {}),
        # Split and merge spend the same daily budget as fills: gas, not trading gas.
        (errors.GasBudgetExceededError(retry_after=123), 429, _BUDGET, _RETRY),
        (errors.NothingToClaimError(), 400, "nothing to claim", {}),
        (errors.TransactionRevertedError(_REVERTED), 400, _REVERTED, {}),
    ],
    ids=["admin-gas-paused", "gas-budget", "nothing-to-claim", "reverted"],
)
def test_gas_and_claim_errors_map_to_their_status_detail_and_headers(
    exc, status, detail, headers
):
    r = _get(exc)
    assert (r.status_code, r.json()["detail"]) == (status, detail)
    for name, value in headers.items():
        assert r.headers[name] == value


@pytest.mark.parametrize(
    ("exc", "status", "detail", "level"),
    [
        # Nothing in the request is wrong and it may be retried: the lock working.
        (errors.TransactionInProgressError(), 409, _IN_PROGRESS, "INFO"),
        # Our side is congested, not the bare 500 an uncaught `TimeExhausted` was.
        (errors.GasTopUpTimeoutError(), 503, _BUSY, "WARNING"),
        # It may well mine, so the caller is told not to repeat it.
        (errors.TransactionPendingError(), 503, _PENDING, "WARNING"),
        # Underpriced twice: the fee is still climbing, not a bare `Web3RPCError` 500.
        (errors.GasPriceMovedError(), 503, _PRICE_MOVED, "WARNING"),
    ],
    ids=["lock-held", "top-up-timed-out", "outcome-unknown", "gas-price-moved"],
)
def test_a_refusal_on_our_side_is_logged_and_not_a_business_rule_error(
    caplog, exc, status, detail, level
):
    caplog.set_level(logging.INFO, logger="agentpit.api.exception_handlers")
    r = _get(exc)
    assert r.status_code == status
    assert r.json() == {"detail": detail}
    ours = [rec for rec in caplog.records if rec.name == "agentpit.api.exception_handlers"]
    assert [rec.levelname for rec in ours] == [level]
    assert not issubclass(type(exc), BusinessRuleError)
