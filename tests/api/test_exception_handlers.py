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

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentpit.api.exception_handlers import register_exception_handlers
from agentpit.domain.exceptions import (
    AdminGasPausedError,
    BusinessRuleError,
    GasBudgetExceededError,
    GasPriceMovedError,
    GasTopUpTimeoutError,
    InsufficientGasError,
    NothingToClaimError,
    TransactionInProgressError,
    TransactionPendingError,
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

    @app.get("/busy")
    def _busy():
        raise GasTopUpTimeoutError()

    @app.get("/price-moved")
    def _price_moved():
        raise GasPriceMovedError()

    @app.get("/pending")
    def _pending():
        raise TransactionPendingError()

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


def test_a_gas_top_up_that_timed_out_is_503_logged_at_warning(caplog):
    """The admin's top-up got no receipt in time (or never found a free admin
    transaction slot): our side is congested, nothing the caller did is wrong,
    and the same request may succeed in a moment. 503 and WARNING, like the
    breaker -- not the bare 500 an uncaught `TimeExhausted` used to be."""
    caplog.set_level(logging.INFO, logger="agentpit.api.exception_handlers")
    client = TestClient(_sponsor_stub_app(), raise_server_exceptions=False)
    r = client.get("/busy")
    assert r.status_code == 503
    assert r.json() == {"detail": "the platform is busy — try again in a moment"}
    ours = [rec for rec in caplog.records if rec.name == "agentpit.api.exception_handlers"]
    assert [rec.levelno for rec in ours] == [logging.WARNING]
    assert not issubclass(GasTopUpTimeoutError, BusinessRuleError)


def test_a_transaction_whose_outcome_is_unknown_is_503_logged_at_warning(caplog):
    """The user's transaction went out and its receipt did not come back in
    time, or the node never answered the broadcast. It may well mine, so the
    caller is told not to repeat it: the pending row puts it in their history
    once it lands. Not a bare 500: nothing failed that the caller could see."""
    caplog.set_level(logging.INFO, logger="agentpit.api.exception_handlers")
    client = TestClient(_sponsor_stub_app(), raise_server_exceptions=False)
    r = client.get("/pending")
    assert r.status_code == 503
    assert r.json() == {
        "detail": "the transaction was sent but is not confirmed yet — it will "
        "appear in your history once it lands; do not repeat it"
    }
    ours = [rec for rec in caplog.records if rec.name == "agentpit.api.exception_handlers"]
    assert [rec.levelno for rec in ours] == [logging.WARNING]
    assert not issubclass(TransactionPendingError, BusinessRuleError)


def test_a_gas_price_that_moved_twice_is_503_logged_at_warning(caplog):
    """The node refused a user's transaction as underpriced, and refused the
    re-sized retry too: the fee is still climbing. Nothing is in flight and
    the same request goes through once it settles, so 503 and WARNING like
    the other congestion on our side -- not the bare 500 the raw
    `Web3RPCError` used to be."""
    caplog.set_level(logging.INFO, logger="agentpit.api.exception_handlers")
    client = TestClient(_sponsor_stub_app(), raise_server_exceptions=False)
    r = client.get("/price-moved")
    assert r.status_code == 503
    assert r.json() == {
        "detail": "the network fee rose while sending — try again in a moment"
    }
    ours = [rec for rec in caplog.records if rec.name == "agentpit.api.exception_handlers"]
    assert [rec.levelno for rec in ours] == [logging.WARNING]
    assert not issubclass(GasPriceMovedError, BusinessRuleError)
