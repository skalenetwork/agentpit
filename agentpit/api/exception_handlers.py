import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from agentpit.auth.workos_client import (
    WorkOsError,
    WorkOsRateLimitedError,
    WorkOsUnavailableError,
)
from agentpit.domain.exceptions import (
    AdminGasPausedError,
    AlreadyExistsError,
    AuthCodeRateLimitedError,
    BusinessRuleError,
    FeatureDisabledError,
    GasBudgetExceededError,
    GasPriceMovedError,
    GasTopUpTimeoutError,
    InsufficientGasError,
    InvalidCredentialsError,
    NotFoundError,
    TransactionInProgressError,
    TransactionPendingError,
)

log = logging.getLogger(__name__)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(AlreadyExistsError)
    async def _already_exists(_: Request, exc: AlreadyExistsError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(InvalidCredentialsError)
    async def _invalid_creds(_: Request, exc: InvalidCredentialsError) -> JSONResponse:
        return JSONResponse(status_code=401, content={"detail": str(exc)})

    @app.exception_handler(AuthCodeRateLimitedError)
    async def _auth_code_rate_limited(
        _: Request, exc: AuthCodeRateLimitedError
    ) -> JSONResponse:
        """Our own ceiling on `POST /auth/code`, not WorkOS's.

        Same status and deliberately the same wording as the WorkOS rate-limit
        handler below, so a caller cannot tell whose ceiling they hit. INFO, not
        WARNING: a limiter refusing a request is the limiter working.
        """
        log.info("auth-code rate limit refused a request: %s", exc)
        return JSONResponse(
            status_code=429,
            content={"detail": str(exc)},
            headers={"Retry-After": str(exc.retry_after)},
        )

    @app.exception_handler(FeatureDisabledError)
    async def _feature_disabled(_: Request, exc: FeatureDisabledError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(GasBudgetExceededError)
    async def _gas_budget(_: Request, exc: GasBudgetExceededError) -> JSONResponse:
        """A per-account limiter working, so INFO -- same as the auth-code limit."""
        log.info("daily gas budget refused a request: %s", exc)
        return JSONResponse(
            status_code=429,
            content={"detail": str(exc)},
            headers={"Retry-After": str(exc.retry_after)},
        )

    @app.exception_handler(TransactionInProgressError)
    async def _transaction_in_progress(
        _: Request, exc: TransactionInProgressError
    ) -> JSONResponse:
        """The per-account transaction lock is held: a second claim, split or
        merge while one is still being sent. The lock working, so INFO."""
        log.info("per-account transaction lock refused a request: %s", exc)
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(AdminGasPausedError)
    async def _admin_gas_paused(_: Request, exc: AdminGasPausedError) -> JSONResponse:
        """Our wallet is low: an outage on our side, so 503 and WARNING."""
        log.warning("admin gas breaker refused a request: %s", exc)
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(GasTopUpTimeoutError)
    async def _gas_top_up_timeout(
        _: Request, exc: GasTopUpTimeoutError
    ) -> JSONResponse:
        """The admin's top-up timed out: congestion on our side, so 503 and
        WARNING, like the breaker."""
        log.warning("gas top-up timed out, a request was refused: %s", exc)
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(GasPriceMovedError)
    async def _gas_price_moved(_: Request, exc: GasPriceMovedError) -> JSONResponse:
        """The node refused a user transaction as underpriced twice: the fee
        is still climbing. Nothing is in flight and nothing the caller did is
        wrong, so 503 and WARNING, like a top-up that timed out."""
        log.warning("the gas price moved twice, a request was refused: %s", exc)
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(TransactionPendingError)
    async def _transaction_pending(
        _: Request, exc: TransactionPendingError
    ) -> JSONResponse:
        """A user transaction went out and its outcome is unknown: no receipt
        in time, or no answer to the broadcast. 503 and WARNING like the other
        congestion on our side; the detail tells the caller not to repeat it."""
        log.warning("a user transaction's outcome is unknown: %s", exc)
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    # Registered ahead of the generic BusinessRuleError handler below it, but
    # order doesn't actually matter to Starlette's lookup -- it walks the
    # raised exception's own MRO and matches the most specific registered
    # type first, so `InsufficientGasError` (a `BusinessRuleError` subclass)
    # always wins over the catch-all regardless of registration order.
    @app.exception_handler(InsufficientGasError)
    async def _insufficient_gas(
        _: Request, exc: InsufficientGasError
    ) -> JSONResponse:
        return JSONResponse(status_code=402, content={"detail": str(exc)})

    @app.exception_handler(BusinessRuleError)
    async def _business_rule(_: Request, exc: BusinessRuleError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    # Registered before its base class for readability only; as with
    # `InsufficientGasError` above, Starlette walks the raised exception's MRO
    # and the subclass wins whatever the order.
    @app.exception_handler(WorkOsUnavailableError)
    async def _workos_unreachable(
        _: Request, exc: WorkOsUnavailableError
    ) -> JSONResponse:
        """WorkOS was never reached: our outage, not the caller's typo.

        Split out of the 401 below because collapsing the two was wrong twice
        over. On POST /auth/code the caller asked to be SENT a code and was not
        -- telling them to request a new one loops forever. And a total sign-in
        outage reported as 4xx is invisible to status-code monitoring, which is
        the one signal that would page anybody; 503 is the same answer the
        unconfigured deployment gives, and means the same thing to a client.
        ERROR, not WARNING: nothing the caller did can fix this.
        """
        log.error("WorkOS is unreachable: %s", exc)
        return JSONResponse(
            status_code=503,
            content={"detail": "sign-in is temporarily unavailable — try again"},
        )

    @app.exception_handler(WorkOsRateLimitedError)
    async def _workos_rate_limited(
        _: Request, exc: WorkOsRateLimitedError
    ) -> JSONResponse:
        """We asked WorkOS too often. Not the caller's typo, not our outage.

        Starlette walks the raised exception's MRO and matches the most
        specific registered type, so this wins over the `WorkOsError` handler
        below regardless of registration order -- the same mechanism
        `InsufficientGasError` relies on.

        WARNING, not ERROR: a rate limit is a working system saying no.
        """
        log.warning("WorkOS rate limited a request: %s", exc)
        return JSONResponse(
            status_code=429,
            content={"detail": "too many attempts — wait a moment and try again"},
        )

    @app.exception_handler(WorkOsError)
    async def _workos_refused(_: Request, exc: WorkOsError) -> JSONResponse:
        """A failed sign-in, not a failed server.

        `WorkOsError` is deliberately one type for every WorkOS failure, so
        everything WorkOS actively refused -- a mistyped code, an expired one,
        a rejected refresh token -- arrives here together. 401 is right for all
        of them, and without this handler the exception is a `RuntimeError`
        that falls through to a 500, turning a six-digit typo into "agentpit is
        broken". The one case that is NOT a refusal, never reaching WorkOS at
        all, is `WorkOsUnavailableError` above.

        Handled here rather than by making `WorkOsError` a subclass of
        `InvalidCredentialsError`: the migration script catches it outside the
        API entirely, and its message must not become the response `detail`.
        That message names the WorkOS endpoint and quotes the (redacted)
        response body -- diagnostics for us, noise for whoever fat-fingered a
        digit -- so it is logged instead.
        """
        log.warning("WorkOS refused a request: %s", exc)
        return JSONResponse(
            status_code=401,
            content={"detail": "could not sign you in — request a new code"},
        )
