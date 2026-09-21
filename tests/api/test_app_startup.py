"""What `create_app` says out loud when it is built without WorkOS.

Since the cutover WorkOS is the only way to sign in, so an api container that
starts without it is a broken deploy -- but a SILENTLY broken one, because the
symptom lands on end users at `/auth/code` rather than on the operator at
startup. Compose cannot catch it either: on the api side these variables
arrive through `env_file`, which has no `:?` form (the UI half IS gated, in
deploy/docker-compose.prod.yml).

The other half of the contract is that it must not raise. Trading bots
authenticate with `X-API-Key`, which has nothing to do with WorkOS, and
refusing to start would take their `/order` down over a sign-in problem.
"""
import logging

from agentpit.api.app import create_app
from agentpit.config import Settings


class _Capture(logging.Handler):
    """Records off the app logger itself.

    `caplog` cannot be used here: `create_app` calls `_configure_root_logging`,
    which does `root.handlers.clear()` and throws pytest's capturing handler
    away mid-call. A handler on the child logger survives that.
    """

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _build(**over) -> list[str]:
    base = {"workos_api_key": "sk_test_123", "workos_client_id": "client_123"}
    base.update(over)
    handler = _Capture()
    handler.setLevel(logging.ERROR)
    logger = logging.getLogger("agentpit.api.app")
    logger.addHandler(handler)
    try:
        create_app(Settings(**base))  # type: ignore[arg-type]
    finally:
        logger.removeHandler(handler)
    return handler.messages


def test_an_unconfigured_workos_is_an_error_naming_the_consequence():
    messages = _build(workos_api_key="", workos_client_id="")

    workos = [m for m in messages if "WorkOS is not configured" in m]
    assert workos, messages
    # The point of the line is the consequence, not the variable names -- an
    # operator reading it should not have to already know what WorkOS gates.
    said = workos[0]
    assert "NOBODY CAN SIGN IN" in said
    assert "private-key export" in said
    # And what still works, so nobody reads this and restarts the bots too.
    assert "X-API-Key" in said


def test_a_missing_api_key_alone_still_trips_it():
    # `build_workos_client` returns None on either variable being absent, so a
    # half-configured deploy is as broken as an empty one and must say so.
    assert any("WorkOS is not configured" in m for m in _build(workos_api_key=""))


def test_a_configured_workos_says_nothing():
    assert not [m for m in _build() if "WorkOS is not configured" in m]


def test_an_unconfigured_workos_does_not_stop_the_app_being_built():
    # The explicit non-goal: `X-API-Key` traffic must survive a WorkOS-less
    # deploy, so this may never become a raise.
    app = create_app(Settings(workos_api_key="", workos_client_id=""))  # type: ignore[arg-type]
    assert app is not None
    # The OpenAPI paths, not `app.routes`: FastAPI 0.141 keeps an included
    # router as one `_IncludedRouter` entry there, hiding every path in it.
    routes = app.openapi()["paths"]
    assert "/order" in routes, "the bots' endpoint must exist regardless"
