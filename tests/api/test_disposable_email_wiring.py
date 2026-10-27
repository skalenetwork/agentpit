"""`get_disposable_email_checker` honours the two kill switches and keeps one
checker per setting combination, so its MX cache survives from request to request.

Settings are built from explicit field values with `_env_file=None`, so what the
environment says (the suite itself sets `AGENTPIT_DISPOSABLE_MX_CHECK=false`)
cannot change the answer.
"""
import pytest

from agentpit.api import deps
from agentpit.auth.disposable_email import DisposableEmailChecker, DnsMxResolver
from agentpit.config import Settings


@pytest.fixture(autouse=True)
def _fresh_checker_cache(monkeypatch):
    # The module keeps its checkers for the life of the process; a test must
    # neither see another's nor leave its own behind.
    monkeypatch.setattr(deps, "_disposable_checkers", {})


def _settings(*, block: bool, mx: bool) -> Settings:
    return Settings(
        _env_file=None, block_disposable_email=block, disposable_mx_check=mx
    )


def test_the_filter_is_off_when_blocking_is_switched_off():
    assert deps.get_disposable_email_checker(_settings(block=False, mx=True)) is None
    assert deps.get_disposable_email_checker(_settings(block=False, mx=False)) is None


def test_no_mx_check_means_a_checker_without_a_resolver():
    checker = deps.get_disposable_email_checker(_settings(block=True, mx=False))

    assert isinstance(checker, DisposableEmailChecker)
    assert checker._resolver is None
    # The vendored list still does its job without DNS.
    assert checker.is_disposable("x@mailinator.com") is True


def test_the_mx_check_uses_the_dns_resolver():
    checker = deps.get_disposable_email_checker(_settings(block=True, mx=True))

    assert isinstance(checker, DisposableEmailChecker)
    assert isinstance(checker._resolver, DnsMxResolver)


def test_repeated_calls_for_the_same_flags_return_the_same_checker():
    first = deps.get_disposable_email_checker(_settings(block=True, mx=False))
    second = deps.get_disposable_email_checker(_settings(block=True, mx=False))
    with_mx = deps.get_disposable_email_checker(_settings(block=True, mx=True))

    assert first is second
    assert with_mx is not first
    assert with_mx is deps.get_disposable_email_checker(_settings(block=True, mx=True))
