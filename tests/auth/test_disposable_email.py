"""The temporary-email check, against a resolver that never touches the network.

Every test but `test_vendored_files_load` and the `DnsMxResolver` ones builds the
checker from literal sets and a `FakeResolver`, so what is pinned here is the
decision rule and the cache -- not the contents of a list that changes upstream
every week. `test_vendored_files_load` is the one place that reads the real
data files, and only to prove they load and keep the common providers out.
"""
import logging

import dns.rdata
import dns.resolver
import pytest

from agentpit.auth import disposable_email
from agentpit.auth.disposable_email import DisposableEmailChecker, DnsMxResolver

LOGGER = "agentpit.auth.disposable_email"


class FakeResolver:
    """A dict of domain -> MX hosts. Domains in `failing` raise instead."""

    def __init__(self, hosts=None, failing=()):
        self.hosts = dict(hosts or {})
        self.failing = set(failing)
        self.calls: list[str] = []

    def mx_hosts(self, domain: str) -> list[str]:
        self.calls.append(domain)
        if domain in self.failing:
            raise RuntimeError("resolver is down")
        return list(self.hosts.get(domain, []))


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _checker(blocked=(), allowed=(), resolver=None, **kwargs) -> DisposableEmailChecker:
    return DisposableEmailChecker(
        frozenset(blocked), frozenset(allowed), resolver, **kwargs
    )


def test_listed_domain_is_disposable():
    assert _checker(blocked={"mailinator.com"}).is_disposable("a@mailinator.com") is True


def test_parent_domains_match():
    checker = _checker(blocked={"mailinator.com"})
    assert checker.is_disposable("a@x.y.mailinator.com") is True


def test_case_is_ignored():
    checker = _checker(blocked={"mailinator.com"})
    assert checker.is_disposable("User@MAILINATOR.COM") is True


def test_allowlist_wins_for_subdomains():
    checker = _checker(blocked={"anonaddy.com"}, allowed={"anonaddy.com"})
    assert checker.is_disposable("me@alice.anonaddy.com") is False


def test_an_allowlisted_address_domain_skips_the_mx_lookup():
    # duck.com forwards to a real inbox even when its mail host is one a
    # temporary-mail service also uses: the allowlist wins, and DNS is not asked.
    resolver = FakeResolver({"duck.com": ["mx.10minutemail.com"]})
    checker = _checker(
        blocked={"10minutemail.com"}, allowed={"duck.com"}, resolver=resolver
    )

    assert checker.is_disposable("me@duck.com") is False
    assert checker.is_disposable("me@sub.duck.com") is False
    assert resolver.calls == []


def test_a_mail_host_that_is_both_blocked_and_allowed_is_not_disposable():
    # An alias service the list carries and the allowlist excuses: a domain whose
    # mail goes to it is a person's alias, not a throwaway inbox.
    resolver = FakeResolver({"example.org": ["mx1.simplelogin.com"]})
    checker = _checker(
        blocked={"simplelogin.com"}, allowed={"simplelogin.com"}, resolver=resolver
    )

    assert checker.is_disposable("me@example.org") is False
    assert resolver.calls == ["example.org"]


def test_mx_on_a_listed_provider_is_disposable():
    resolver = FakeResolver({"gonrr.net": ["prd-smtp.10minutemail.com"]})
    checker = _checker(blocked={"10minutemail.com"}, resolver=resolver)
    assert checker.is_disposable("x@gonrr.net") is True


def test_mx_on_an_ordinary_provider_is_allowed():
    resolver = FakeResolver({"example.org": ["aspmx.l.google.com"]})
    checker = _checker(blocked={"10minutemail.com"}, resolver=resolver)
    assert checker.is_disposable("x@example.org") is False


def test_no_mx_is_allowed():
    resolver = FakeResolver({"example.org": []})
    checker = _checker(blocked={"10minutemail.com"}, resolver=resolver)
    assert checker.is_disposable("x@example.org") is False


def test_resolver_failure_allows_and_is_not_cached_as_blocked(caplog):
    clock = FakeClock()
    resolver = FakeResolver(failing={"gonrr.net"})
    checker = _checker(blocked={"10minutemail.com"}, resolver=resolver, clock=clock)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert checker.is_disposable("x@gonrr.net") is False

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("gonrr.net" in r.getMessage() for r in warnings)

    # The outage ends and the domain turns out to be a disposable one; the
    # failure was only remembered for a minute, so the next minute asks again.
    resolver.failing.clear()
    resolver.hosts["gonrr.net"] = ["prd-smtp.10minutemail.com"]
    clock.now += 61
    assert checker.is_disposable("x@gonrr.net") is True
    assert resolver.calls == ["gonrr.net", "gonrr.net"]


def test_a_resolver_failure_is_remembered_as_allowed_for_a_minute():
    # During an outage every attempt would otherwise wait out the lookup's
    # deadline on a shared thread, so a failure is answered from the cache.
    clock = FakeClock()
    resolver = FakeResolver(failing={"gonrr.net"})
    checker = _checker(blocked={"10minutemail.com"}, resolver=resolver, clock=clock)

    assert checker.is_disposable("x@gonrr.net") is False
    clock.now += 59
    assert checker.is_disposable("y@gonrr.net") is False
    assert resolver.calls == ["gonrr.net"]

    clock.now += 2  # 61 seconds after the failure
    assert checker.is_disposable("z@gonrr.net") is False
    assert resolver.calls == ["gonrr.net", "gonrr.net"]


def test_the_failure_ttl_is_its_own_setting():
    clock = FakeClock()
    resolver = FakeResolver(failing={"gonrr.net"})
    checker = _checker(
        blocked={"10minutemail.com"},
        resolver=resolver,
        clock=clock,
        ttl_seconds=86400,
        failure_ttl_seconds=5,
    )

    checker.is_disposable("x@gonrr.net")
    clock.now += 6
    checker.is_disposable("x@gonrr.net")
    assert resolver.calls == ["gonrr.net", "gonrr.net"]


def test_verdict_says_listed_for_a_blocklisted_address_domain():
    checker = _checker(blocked={"mailinator.com"})
    assert checker.verdict("a@x.mailinator.com") == "listed"


def test_verdict_names_the_listed_domain_of_the_mail_host():
    resolver = FakeResolver({"gonrr.net": ["mx1.example.org", "prd-smtp.10minutemail.com"]})
    checker = _checker(blocked={"10minutemail.com"}, resolver=resolver)

    assert checker.verdict("x@gonrr.net") == "mx:10minutemail.com"
    # Served from the cache the second time, with the same reason.
    assert checker.verdict("y@gonrr.net") == "mx:10minutemail.com"
    assert resolver.calls == ["gonrr.net"]


def test_verdict_is_none_for_an_allowed_address():
    resolver = FakeResolver({"example.org": ["aspmx.l.google.com"]})
    checker = _checker(
        blocked={"10minutemail.com"}, allowed={"duck.com"}, resolver=resolver
    )

    assert checker.verdict("x@example.org") is None
    assert checker.verdict("x@duck.com") is None
    assert checker.verdict("nobody") is None


def test_verdicts_are_cached():
    resolver = FakeResolver({"example.org": ["aspmx.l.google.com"]})
    checker = _checker(resolver=resolver)

    assert checker.is_disposable("a@example.org") is False
    assert checker.is_disposable("b@example.org") is False
    assert resolver.calls == ["example.org"]


def test_cache_expires_after_ttl():
    clock = FakeClock()
    resolver = FakeResolver({"example.org": ["aspmx.l.google.com"]})
    checker = _checker(resolver=resolver, ttl_seconds=10, clock=clock)

    checker.is_disposable("a@example.org")
    clock.now += 9
    checker.is_disposable("a@example.org")
    assert resolver.calls == ["example.org"]

    clock.now += 2  # 11 seconds after the lookup
    checker.is_disposable("a@example.org")
    assert resolver.calls == ["example.org", "example.org"]


def test_cache_is_bounded():
    resolver = FakeResolver()
    checker = _checker(resolver=resolver, max_entries=2)

    for address in ("x@a.test", "x@b.test", "x@c.test", "x@a.test"):
        checker.is_disposable(address)

    # c.test pushed a.test out, so the last query went to DNS again.
    assert resolver.calls == ["a.test", "b.test", "c.test", "a.test"]


def test_without_a_resolver_only_the_list_counts():
    checker = _checker(blocked={"mailinator.com"}, resolver=None)
    assert checker.is_disposable("a@mailinator.com") is True
    assert checker.is_disposable("a@example.org") is False


def test_an_address_without_at_sign_is_not_disposable():
    resolver = FakeResolver()
    checker = _checker(blocked={"mailinator.com"}, resolver=resolver)
    assert checker.is_disposable("nobody") is False
    assert resolver.calls == []


def test_vendored_files_load():
    checker = DisposableEmailChecker.from_files(None)

    assert checker.is_disposable("x@mailinator.com") is True
    assert checker.is_disposable("x@10minutemail.com") is True
    for address in (
        "x@gmail.com",
        "x@privaterelay.appleid.com",
        "x@duck.com",
        "x@mozmail.com",
    ):
        assert checker.is_disposable(address) is False, address


def _mx_answer(*exchanges: str) -> list:
    return [dns.rdata.from_text("IN", "MX", f"10 {e}") for e in exchanges]


def test_dns_resolver_returns_lowercase_hosts_without_the_trailing_dot(monkeypatch):
    seen = {}

    def fake_resolve(domain, rdtype, **kwargs):
        seen.update(domain=domain, rdtype=rdtype, **kwargs)
        return _mx_answer("Prd-SMTP.10MinuteMail.com.", "mx2.example.org.")

    monkeypatch.setattr(disposable_email.dns.resolver, "resolve", fake_resolve)

    hosts = DnsMxResolver(lifetime=1.5).mx_hosts("gonrr.net")

    assert hosts == ["prd-smtp.10minutemail.com", "mx2.example.org"]
    assert seen == {"domain": "gonrr.net", "rdtype": "MX", "lifetime": 1.5}


@pytest.mark.parametrize("error", [dns.resolver.NoAnswer, dns.resolver.NXDOMAIN])
def test_dns_resolver_treats_no_mx_and_no_domain_as_an_empty_answer(monkeypatch, error):
    def fake_resolve(domain, rdtype, **kwargs):
        raise error()

    monkeypatch.setattr(disposable_email.dns.resolver, "resolve", fake_resolve)

    assert DnsMxResolver().mx_hosts("example.org") == []


def test_dns_resolver_treats_a_null_mx_as_no_mail_host(monkeypatch):
    monkeypatch.setattr(
        disposable_email.dns.resolver,
        "resolve",
        lambda domain, rdtype, **kwargs: _mx_answer("."),
    )
    assert DnsMxResolver().mx_hosts("example.org") == []


def test_dns_resolver_lets_every_other_failure_propagate(monkeypatch):
    def fake_resolve(domain, rdtype, **kwargs):
        raise dns.resolver.LifetimeTimeout(timeout=2.0, errors=[])

    monkeypatch.setattr(disposable_email.dns.resolver, "resolve", fake_resolve)

    with pytest.raises(dns.resolver.LifetimeTimeout):
        DnsMxResolver().mx_hosts("example.org")
