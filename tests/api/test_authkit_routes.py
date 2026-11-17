"""Signing in with a mailed code, over the real app."""
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from agentpit.api import deps
from agentpit.api.main import app
from agentpit.auth.workos_client import (
    FakeWorkOsClient,
    RealWorkOsClient,
    WorkOsUnavailableError,
)
from agentpit.config import Settings
from agentpit.db.session import DbSession
from tests.db_helpers import fresh_test_conn


@contextmanager
def _using(client):
    """Swap the app's WorkOS client, then put back exactly what was there.

    Restore rather than pop, the way `_isolated_db_session` in conftest handles
    `get_db_session`. Something else already owns this key -- `create_app`
    installs the production client under it and conftest replaces that with the
    offline default -- so popping does not undo this override, it deletes
    theirs, and every later test in the session that touches an /auth route
    without asking for the `workos` fixture would 500 on the placeholder's
    RuntimeError, looking like a product bug in a file that changed nothing.
    """
    previous = app.dependency_overrides.get(deps.get_workos_client)
    app.dependency_overrides[deps.get_workos_client] = lambda: client
    try:
        yield client
    finally:
        if previous is None:
            app.dependency_overrides.pop(deps.get_workos_client, None)
        else:
            app.dependency_overrides[deps.get_workos_client] = previous


@pytest.fixture
def workos():
    with _using(FakeWorkOsClient()) as fake:
        yield fake




def _code(workos: FakeWorkOsClient, email: str) -> str:
    return workos.last_code(email)


def test_post_auth_code_accepts_any_address_with_202(workos):
    with TestClient(app) as client:
        resp = client.post("/auth/code", json={"email": "brand-new@example.com"})
    assert resp.status_code == 202, resp.text


def test_post_auth_code_says_the_same_thing_for_known_and_unknown_addresses(workos):
    # Whether an address has an account must not be inferable here: anybody
    # can post any address. `/register`'s 409 used to leak exactly that, and
    # since the cutover retired it this endpoint is the only one a stranger
    # can ask -- so it is now the whole of the answer, not merely a second
    # oracle beside an existing one.
    with TestClient(app) as client:
        first = client.post("/auth/code", json={"email": "known@example.com"})
        client.post(
            "/auth/session",
            json={"email": "known@example.com", "code": _code(workos, "known@example.com")},
        )
        # Age the per-address window first. The rate limit answers 429 for an
        # address asked twice inside a minute, and that IS distinguishable --
        # but what it distinguishes is recent activity, not whether an account
        # exists, and it applies identically to addresses that have one and
        # addresses that do not. Ageing it here keeps this test measuring the
        # oracle it was written for rather than the limiter.
        again = client.post("/auth/code", json={"email": "known@example.com"})
        stranger = client.post("/auth/code", json={"email": "stranger@example.com"})
    assert first.status_code == again.status_code == stranger.status_code == 202
    assert again.json() == stranger.json()


def test_the_right_code_returns_a_session_and_a_funded_account(workos):
    with TestClient(app) as client:
        client.post("/auth/code", json={"email": "a@example.com"})
        resp = client.post(
            "/auth/session",
            json={"email": "a@example.com", "code": _code(workos, "a@example.com")},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["access_token"] and body["refresh_token"]
    # The wallet is the thing WorkOS cannot make for us.
    assert body["user"]["eth_address"].startswith("0x")
    assert body["user"]["email"] == "a@example.com"


def test_a_wrong_code_is_401_and_creates_nothing(workos):
    with TestClient(app) as client:
        client.post("/auth/code", json={"email": "b@example.com"})
        resp = client.post(
            "/auth/session", json={"email": "b@example.com", "code": "000000"}
        )
        assert resp.status_code == 401, resp.text
        # Look, rather than infer it from the 200 below: if a failed
        # `authenticate_with_code` ever did commit a row -- a refactor that
        # resolved the account before calling WorkOS would -- the correct code
        # afterwards finds that row by WORKOS_USER_ID and still answers 200,
        # so the second half of this test passes while a wrong code has minted
        # a funded wallet for whoever guessed the address.
        with fresh_test_conn() as conn:
            made = conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE EMAIL = %s",
                ("b@example.com",),
            ).fetchone()
        assert made["n"] == 0, made
        # Nothing was created, so a correct code afterwards still works.
        ok = client.post(
            "/auth/session",
            json={"email": "b@example.com", "code": _code(workos, "b@example.com")},
        )
    assert ok.status_code == 200, ok.text


def test_a_malformed_code_is_422_and_never_reaches_workos(workos):
    with TestClient(app) as client:
        client.post("/auth/code", json={"email": "c@example.com"})
        resp = client.post(
            "/auth/session", json={"email": "c@example.com", "code": "12345"}
        )
    assert resp.status_code == 422
    # The status code alone does not say WorkOS was spared: a round trip that
    # came back refused would be a 401, which is also not 200. Count the calls
    # instead -- this is the assertion the test's name makes.
    assert workos.authenticate_calls == 0


def test_signing_in_twice_returns_the_same_account(workos):
    with TestClient(app) as client:
        client.post("/auth/code", json={"email": "d@example.com"})
        first = client.post(
            "/auth/session",
            json={"email": "d@example.com", "code": _code(workos, "d@example.com")},
        ).json()
        client.post("/auth/code", json={"email": "d@example.com"})
        second = client.post(
            "/auth/session",
            json={"email": "d@example.com", "code": _code(workos, "d@example.com")},
        ).json()
    assert first["user"]["user_id"] == second["user"]["user_id"]
    assert first["user"]["eth_address"] == second["user"]["eth_address"]


def test_an_address_whose_workos_user_is_gone_signs_in_to_its_account_again(workos):
    # What a `DELETE /me` that failed after its WorkOS call leaves: the WorkOS
    # user is gone and the row still holds the address. From then on both doors
    # answered 401 for that address, the callback page as an expired link.
    with TestClient(app) as client:
        client.post("/auth/code", json={"email": "back@example.com"})
        first = client.post(
            "/auth/session",
            json={"email": "back@example.com", "code": _code(workos, "back@example.com")},
        ).json()
        workos.delete_user(workos.find_user_by_email("back@example.com").workos_user_id)

        by_google = client.post(
            "/auth/callback",
            json={"code": workos.issue_authorization_code("back@example.com")},
        )
        client.post("/auth/code", json={"email": "back@example.com"})
        by_code = client.post(
            "/auth/session",
            json={"email": "back@example.com", "code": _code(workos, "back@example.com")},
        )
    assert by_google.status_code == 200, by_google.text
    assert by_code.status_code == 200, by_code.text
    assert by_google.json()["user"]["user_id"] == first["user"]["user_id"]
    assert by_code.json()["user"]["user_id"] == first["user"]["user_id"]


def test_refresh_returns_a_new_access_token_for_the_same_user(workos):
    with TestClient(app) as client:
        client.post("/auth/code", json={"email": "e@example.com"})
        session = client.post(
            "/auth/session",
            json={"email": "e@example.com", "code": _code(workos, "e@example.com")},
        ).json()
        resp = client.post(
            "/auth/refresh", json={"refresh_token": session["refresh_token"]}
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["user_id"] == session["user"]["user_id"]


def test_a_garbage_refresh_token_is_401(workos):
    with TestClient(app) as client:
        resp = client.post("/auth/refresh", json={"refresh_token": "nonsense"})
    assert resp.status_code == 401, resp.text


def test_the_routes_answer_503_when_workos_is_not_configured():
    # Every developer machine before the account existed, and any deployment
    # that forgets the keys. It must be an obvious 503, not a 500.
    with _using(None):
        with TestClient(app) as client:
            resp = client.post("/auth/code", json={"email": "f@example.com"})
    assert resp.status_code == 503, resp.text


def test_an_unreachable_workos_is_503_not_401():
    """An outage is ours, and must not read as "you typed it wrong".

    /auth/code is the case that makes it visible: the caller asked to be SENT a
    code and was not, so "request a new code" -- the 401 body -- asks them to
    do the thing that just failed, forever. 503 also puts a sign-in outage
    where status-code monitoring can see it.
    """
    class Unreachable(FakeWorkOsClient):
        def send_magic_auth_code(self, email: str) -> None:
            raise WorkOsUnavailableError("WorkOS request failed: POST /x")

    with _using(Unreachable()):
        with TestClient(app) as client:
            resp = client.post("/auth/code", json={"email": "g@example.com"})
    assert resp.status_code == 503, resp.text
    assert "new code" not in resp.json()["detail"]


def test_the_app_factory_wires_the_workos_client_into_the_dependency():
    """The thing every test above overrides has to exist underneath.

    All of them install their own `get_workos_client`, so all of them stay
    green if `create_app` stops installing it -- verified by deleting that line
    -- while every production POST /auth/code answers 500 from the placeholder's
    RuntimeError. Nothing else in the suite looks at the real wiring, so assert
    it on a freshly built app rather than on the shared, overridden one. No
    request is made: building the client only opens a connection pool.
    """
    from agentpit.api.app import create_app

    configured = create_app(
        Settings(workos_api_key="sk_test_wiring", workos_client_id="client_wiring")
    )
    assert deps.get_workos_client in configured.dependency_overrides
    assert isinstance(
        configured.dependency_overrides[deps.get_workos_client](), RealWorkOsClient
    )

    # And the deployment without keys gets None through the same wire, which is
    # what turns the three routes into the 503 above instead of a 500.
    absent = create_app(Settings(workos_api_key="", workos_client_id=""))
    assert absent.dependency_overrides[deps.get_workos_client]() is None


def test_a_workos_rate_limit_reaches_the_caller_as_429():
    # The UI already has copy for 429 (`codeFlow.ts`), and it has been
    # unreachable because the status never arrived. This is what switches it on.
    from agentpit.auth.workos_client import WorkOsRateLimitedError

    class _RateLimited:
        def send_magic_auth_code(self, email: str) -> None:
            raise WorkOsRateLimitedError("WorkOS POST /user_management/magic_auth returned 429")

    # `_using` rather than a bare override/pop: a bare `pop` at teardown
    # deletes conftest's shared default instead of restoring it, which is
    # invisible right up until some OTHER endpoint that also depends on
    # `get_workos_client` runs later in the session and 500s on the
    # placeholder's RuntimeError -- see `_using`'s own docstring.
    with _using(_RateLimited()):
        with TestClient(app) as client:
            resp = client.post("/auth/code", json={"email": "g@example.com"})
        assert resp.status_code == 429, resp.text
        # The diagnostic message names our endpoint; it is logged, not returned.
        assert "user_management" not in resp.text


# --- /auth/callback: a provider redirect landing back on us ---------------


def test_post_auth_callback_returns_a_session(workos):
    code = workos.issue_authorization_code("cb@example.com")
    with TestClient(app) as client:
        resp = client.post("/auth/callback", json={"code": code})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["access_token"] and body["refresh_token"]
    assert body["user"]["eth_address"].startswith("0x")


def test_a_bad_authorization_code_is_401(workos):
    with TestClient(app) as client:
        resp = client.post("/auth/callback", json={"code": "not-a-code"})
    assert resp.status_code == 401, resp.text


def test_an_empty_authorization_code_is_422_and_never_reaches_workos(workos):
    with TestClient(app) as client:
        resp = client.post("/auth/callback", json={"code": ""})
    assert resp.status_code == 422
    assert workos.authenticate_calls == 0
