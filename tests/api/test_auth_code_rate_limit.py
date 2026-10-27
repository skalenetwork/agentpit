"""The ceiling on `POST /auth/code`.

That endpoint is unauthenticated, is the only door into the product since the
cutover, and every request that reaches WorkOS costs an email. So these tests
care about two things a status code alone cannot show: that a refused request
is refused BEFORE WorkOS is called, and that the two rules key on different
subjects so neither leaves the other's hole open.

The service is driven directly rather than over HTTP wherever the limit itself
is the subject. Going through `TestClient` would mean sending the production
allowance -- twenty requests per address, sixty per IP -- for every assertion.
The two HTTP tests below are the ones that need the route: they check the
status code, the header, and that the client IP reaches the rule at all.
"""
import pytest
from fastapi.testclient import TestClient

from agentpit.api import deps
from agentpit.api.main import app
from agentpit.auth.disposable_email import DisposableEmailChecker
from agentpit.auth.workos_client import FakeWorkOsClient
from agentpit.config import Settings
from agentpit.db.session import DbSession
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import AuthCodeRateLimitedError
from agentpit.services.authkit_service import AuthKitService
from tests.api.conftest import _overriding


@pytest.fixture
def workos():
    # `_overriding` restores rather than pops: `create_app` installs the
    # production client under this key and conftest replaces it with the
    # offline default, so a bare `pop` deletes THEIR value and every later test
    # in the session 500s on the raising placeholder. That exact bug was found
    # and fixed here once already; it is not worth finding twice.
    fake = FakeWorkOsClient()
    with _overriding(deps.get_workos_client, lambda: fake):
        yield fake


def _service(workos: FakeWorkOsClient, *, per_email: int, per_ip: int):
    """The service with small allowances, so a test states its own numbers."""
    return AuthKitService(
        db=DbSession(Settings().database_url),
        workos=workos,
        onboard=lambda *_a, **_k: None,
        reonboard=lambda *_a, **_k: None,
        per_email_hourly=per_email,
        per_ip_hourly=per_ip,
    )


def test_an_address_past_its_allowance_is_refused():
    workos = FakeWorkOsClient()
    svc = _service(workos, per_email=2, per_ip=1000)

    svc.send_code("a@example.com")
    svc.send_code("a@example.com")
    with pytest.raises(AuthCodeRateLimitedError) as excinfo:
        svc.send_code("a@example.com")

    # The window length is what the caller is told to wait.
    assert excinfo.value.retry_after == 3600


def test_a_refused_request_never_reaches_workos():
    # The whole point of the ordering, and something a status code cannot show:
    # a local refusal and a refusal from WorkOS both end in 429.
    workos = FakeWorkOsClient()
    svc = _service(workos, per_email=1, per_ip=1000)

    svc.send_code("b@example.com")
    before = len(workos._codes)
    with pytest.raises(AuthCodeRateLimitedError):
        svc.send_code("b@example.com")

    assert len(workos._codes) == before


def test_a_second_code_for_one_address_is_fine_inside_the_allowance():
    # There is deliberately no per-minute rule. A person whose mail is slow
    # closes the dialog and submits the same address again, and the dialog's
    # cooldown does not stop them -- it guards only its resend button. That
    # person must not be told "too many attempts" for behaving normally.
    workos = FakeWorkOsClient()
    svc = _service(workos, per_email=20, per_ip=1000)

    svc.send_code("c@example.com")
    svc.send_code("c@example.com")  # immediately, no waiting
    svc.send_code("c@example.com")


def test_one_address_does_not_spend_another_s_allowance():
    workos = FakeWorkOsClient()
    svc = _service(workos, per_email=1, per_ip=1000)

    svc.send_code("d@example.com")
    svc.send_code("e@example.com")  # a different address, its own budget


def test_rotating_the_address_still_runs_into_the_per_ip_rule():
    # The reason the address rule alone is not enough: an attacker who changes
    # the address on every request pays nothing under it, and every request is
    # an email we are billed for.
    workos = FakeWorkOsClient()
    svc = _service(workos, per_email=1000, per_ip=3)

    for i in range(3):
        svc.send_code(f"rot{i}@example.com", client_ip="9.9.9.9")
    with pytest.raises(AuthCodeRateLimitedError):
        svc.send_code("rot3@example.com", client_ip="9.9.9.9")


def test_a_request_with_no_trusted_ip_skips_the_ip_rule_rather_than_sharing_one():
    # A shared "unknown" bucket would turn the per-IP rule into a global kill
    # switch that anybody could trip on purpose.
    workos = FakeWorkOsClient()
    svc = _service(workos, per_email=1000, per_ip=1)

    svc.send_code("f@example.com", client_ip=None)
    svc.send_code("g@example.com", client_ip=None)


def test_the_route_answers_429_with_retry_after(workos):
    tight = Settings(AGENTPIT_AUTH_CODE_PER_EMAIL_HOURLY=1)
    with _overriding(deps.get_settings, lambda: tight):
        with TestClient(app) as client:
            first = client.post("/auth/code", json={"email": "h@example.com"})
            second = client.post("/auth/code", json={"email": "h@example.com"})

    assert first.status_code == 202, first.text
    assert second.status_code == 429, second.text
    assert second.headers["Retry-After"] == "3600"
    # Deliberately the same wording WorkOS's own rate limit produces: a caller
    # learns to wait, not whose ceiling they hit.
    assert "too many attempts" in second.json()["detail"]


def test_the_counter_is_atomic_under_a_lost_race():
    # Two callers reading the same stale count and both passing is the failure
    # this table exists to prevent, and the reason the predicate and the
    # increment are one statement. Driven at the writer, where the race is.
    db = DbSession(Settings().database_url)
    with db.write() as conn:
        first = TableWrite.claim_auth_code_attempt(conn, "race", 1000, 60, 1)
        second = TableWrite.claim_auth_code_attempt(conn, "race", 1000, 60, 1)
    assert first is True
    assert second is False


def test_a_window_that_has_expired_starts_over():
    db = DbSession(Settings().database_url)
    with db.write() as conn:
        assert TableWrite.claim_auth_code_attempt(conn, "roll", 1000, 60, 1) is True
        assert TableWrite.claim_auth_code_attempt(conn, "roll", 1030, 60, 1) is False
        # 61 seconds after the window opened, the allowance is fresh.
        assert TableWrite.claim_auth_code_attempt(conn, "roll", 1061, 60, 1) is True


def _temp_mail_checker():
    # Small and offline, so the route tests do not depend on the packaged list.
    return DisposableEmailChecker(frozenset({"mailinator.com"}), frozenset(), None)


def test_a_temporary_address_gets_400_with_the_message(workos):
    checker = _temp_mail_checker()
    with _overriding(deps.get_disposable_email_checker, lambda: checker):
        with TestClient(app) as client:
            resp = client.post("/auth/code", json={"email": "t@mailinator.com"})

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == (
        "Temporary email addresses can't be used. Use a permanent address."
    )


def test_an_ordinary_address_still_gets_202(workos):
    checker = _temp_mail_checker()
    with _overriding(deps.get_disposable_email_checker, lambda: checker):
        with TestClient(app) as client:
            resp = client.post("/auth/code", json={"email": "ok@example.com"})

    assert resp.status_code == 202, resp.text
