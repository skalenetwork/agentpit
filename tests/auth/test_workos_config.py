"""The WorkOS settings must reject at startup what they cannot diagnose later.

Every failure covered here has the same shape: a value an operator plausibly
pastes, which leaves the feature looking configured while making every single
sign-in fail as "invalid session". `AuthKitVerifier.verify` collapses all
failures into one error on purpose -- so a caller cannot probe -- which also
means a configuration mistake is indistinguishable from a forged token once
requests are flowing. The only place to catch it is here.
"""
import logging

from agentpit.auth.workos_client import _redact
from agentpit.config import Settings


def _settings(**over) -> Settings:
    base = {
        "workos_api_key": "sk_test_123",
        "workos_client_id": "client_123",
        "workos_authkit_domain": "https://example.authkit.app",
    }
    base.update(over)
    return Settings(**base)  # type: ignore[arg-type]


def test_trailing_whitespace_is_stripped():
    # Compose's env_file parser leaves it; the same measured reason
    # `_strip_google_client_id` exists.
    s = _settings(
        workos_api_key="sk_test_123 ",
        workos_client_id=" client_123",
        workos_authkit_domain=" https://example.authkit.app ",
    )
    assert s.workos_api_key == "sk_test_123"
    assert s.workos_client_id == "client_123"
    assert s.workos_authkit_domain == "https://example.authkit.app"


def test_a_trailing_slash_on_the_domain_is_removed():
    # The nastiest of the three, because it half-works: the JWKS fetch strips
    # the slash and resolves a key, so the configuration looks right, while the
    # `iss` comparison keeps it and rejects every token.
    s = _settings(workos_authkit_domain="https://example.authkit.app/")
    assert s.workos_authkit_domain == "https://example.authkit.app"


def test_a_domain_without_a_scheme_complains_but_does_not_raise(caplog):
    """A cosmetic misconfiguration must not be able to take the API down.

    This used to raise. `Settings()` is built by `create_app` before anything
    serves, so raising crash-looped the api container -- and it did so over a
    value SPA sign-in does not read: its issuer and JWKS URL both derive from
    `workos_client_id` (`authkit_issuer` / `authkit_jwks_url`). A schemeless
    domain only leaves /mcp off; it must not cost every trading bot `/order`.
    """
    with caplog.at_level(logging.ERROR, logger="agentpit.config"):
        s = _settings(workos_authkit_domain="example.authkit.app")

    # The value is preserved as given -- this validator no longer invents a
    # scheme, it only says the value looks wrong.
    assert s.workos_authkit_domain == "example.authkit.app"
    assert any(
        "WORKOS_AUTHKIT_DOMAIN" in r.getMessage() for r in caplog.records
    ), caplog.text


def test_a_good_domain_still_round_trips():
    # The other half of the above: dropping the raise must not have cost the
    # validator its actual job.
    s = _settings(workos_authkit_domain="https://example.authkit.app")
    assert s.workos_authkit_domain == "https://example.authkit.app"


def test_an_empty_domain_stays_empty_and_does_not_raise():
    # Absent means "the feature is off", which is every machine without a
    # WorkOS account. It must not be an error.
    assert _settings(workos_authkit_domain="").workos_authkit_domain == ""


def test_redaction_leaves_an_ordinary_body_alone():
    body = '{"message":"email already exists","code":"email_taken"}'
    assert _redact(body) == body
