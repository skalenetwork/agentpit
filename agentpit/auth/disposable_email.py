"""Telling a temporary-email address from a real one, before a code is mailed.

Sign-in is passwordless: an address is the whole identity, and every new one
mints a wallet, an API key and an on-chain onboarding. A throwaway inbox
therefore costs the platform money and the person behind it nothing, which is
the one asymmetry worth closing here.

Two checks, in order of cost:

1. The address's own domain, against the blocklist from the
   `disposable-email-domains` package, the packaged mirror of
   github.com/disposable-email-domains/disposable-email-domains. Free, and it
   catches the well-known services.
2. The domain's MX hosts, against the same list. Temporary-mail services keep
   minting fresh domains that all deliver to a few mail servers, so a domain
   missing from the list is still caught when its mail goes to one that is on it.

The MX answer is cached for a day -- a domain's mail server does not change
between two sign-in attempts, and the lookup is the only network call here.

Fail open, deliberately. This is an abuse filter, not an authentication step,
and a DNS hiccup that locked real people out of signing in would cost more than
the throwaway address it was meant to stop. Any resolver failure allows the
address and logs a warning. The failure is remembered as "allowed" for a minute
only, never as disposable: long enough that an outage does not make every
sign-in and refresh wait out the lookup's deadline on a shared thread, short
enough that the next minute asks again.

`data/disposable_allowlist.txt` wins over the blocklist, for the address's own
domain (no MX lookup at all) and for any mail host it points to: forwarding
services (Apple's hide-my-email, SimpleLogin, addy.io, Firefox Relay,
DuckDuckGo) hand out addresses that look disposable and are not, since a real
inbox sits behind them. The blocklist is never fetched at runtime: it ships with
the pinned package and is updated by bumping that pin, so what runs is what was
reviewed.
"""
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Protocol

import dns.resolver
from disposable_email_domains import blocklist

log = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent / "data"


class MxResolver(Protocol):
    def mx_hosts(self, domain: str) -> list[str]:
        """The domain's mail-exchanger names: lower-case, no trailing dot.

        Empty for a domain with no MX record or no such domain -- both mean
        "no mail host to judge", not a failure. Raises for anything else
        (timeout, SERVFAIL, no network), so the caller can tell "nothing there"
        from "could not find out".
        """
        ...


class DnsMxResolver:
    """`MxResolver` over the system's DNS, bounded by one overall deadline."""

    def __init__(self, lifetime: float = 2.0) -> None:
        # `lifetime` caps the whole lookup, retries and fallbacks included;
        # sign-in waits on this, so it cannot be left to the library default.
        self._lifetime = lifetime

    def mx_hosts(self, domain: str) -> list[str]:
        try:
            answer = dns.resolver.resolve(domain, "MX", lifetime=self._lifetime)
        except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
            return []
        hosts = (record.exchange.to_text().rstrip(".").lower() for record in answer)
        # A null MX (RFC 7505, exchange ".") says the domain takes no mail.
        return [host for host in hosts if host]


def _suffixes(name: str) -> Iterator[str]:
    """`a.b.c`, `b.c`, `c`: the name and each of its parent domains."""
    labels = name.split(".")
    for start in range(len(labels)):
        yield ".".join(labels[start:])


def email_domain(email: str) -> str:
    """The lower-case domain after the last "@", without a trailing dot; empty
    when there is no "@" or nothing after it. The one normalisation the checker
    judges and the service logs, so a refusal's log line names what was judged."""
    if "@" not in email:
        return ""
    return email.rsplit("@", 1)[1].lower().rstrip(".")


def _load(path: Path) -> frozenset[str]:
    lines = (line.strip().lower() for line in path.read_text(encoding="utf-8").splitlines())
    return frozenset(line for line in lines if line and not line.startswith("#"))


class DisposableEmailChecker:
    def __init__(
        self,
        blocked: frozenset[str],
        allowed: frozenset[str],
        resolver: MxResolver | None,
        *,
        ttl_seconds: float = 86400,
        failure_ttl_seconds: float = 60,
        max_entries: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._blocked = blocked
        self._allowed = allowed
        self._resolver = resolver
        self._ttl = ttl_seconds
        self._failure_ttl = failure_ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        # domain -> (verdict, expires_at), least recently used first; the
        # verdict is `verdict()`'s answer, None for "allowed". Sign-in runs on a
        # thread pool, so every touch of it holds the lock; the DNS call itself
        # does not, or one slow lookup would stall every sign-in.
        self._cache: OrderedDict[str, tuple[str | None, float]] = OrderedDict()
        self._lock = threading.Lock()

    @classmethod
    def standard(cls, resolver: MxResolver | None) -> "DisposableEmailChecker":
        """The production checker: the packaged blocklist, our alias allowlist."""
        return cls(
            frozenset(blocklist),
            _load(_DATA_DIR / "disposable_allowlist.txt"),
            resolver,
        )

    def is_disposable(self, email: str) -> bool:
        return self.verdict(email) is not None

    def verdict(self, email: str) -> str | None:
        """None when the address is allowed, otherwise why it is not.

        "listed" is the address's own domain being on the blocklist;
        "mx:<domain>" is its mail going to a host under that blocklisted domain.
        A short reason for a log line, never the address itself.
        """
        domain = email_domain(email)
        if not domain:
            return None
        # The allowlist wins outright: no list check, no MX lookup, no cache.
        if self._allowlisted(domain):
            return None
        if self._blocked_by(domain) is not None:
            return "listed"
        if self._resolver is None:
            return None

        hit, cached = self._cached(domain)
        if hit:
            return cached

        try:
            hosts = self._resolver.mx_hosts(domain)
        except Exception as exc:
            log.warning("MX lookup for %s failed, allowing the address: %r", domain, exc)
            # Allowed, and only for a short while: an outage must not turn into
            # a day of verdicts, and must not make every attempt wait it out.
            self._remember(domain, None, self._failure_ttl)
            return None

        reason = next(
            (f"mx:{blocked}" for host in hosts if (blocked := self._blocked_by(host))),
            None,
        )
        self._remember(domain, reason, self._ttl)
        return reason

    def _allowlisted(self, name: str) -> bool:
        return any(s in self._allowed for s in _suffixes(name))

    def _blocked_by(self, name: str) -> str | None:
        """The blocklisted domain `name` is on or under, or None. A name under an
        allowlisted domain is never blocked."""
        if self._allowlisted(name):
            return None
        return next((s for s in _suffixes(name) if s in self._blocked), None)

    def _cached(self, domain: str) -> tuple[bool, str | None]:
        """(hit, verdict): a cached "allowed" is (True, None), a miss (False, None)."""
        with self._lock:
            entry = self._cache.get(domain)
            if entry is None:
                return False, None
            verdict, expires_at = entry
            if self._clock() >= expires_at:
                del self._cache[domain]
                return False, None
            self._cache.move_to_end(domain)
            return True, verdict

    def _remember(self, domain: str, verdict: str | None, ttl: float) -> None:
        with self._lock:
            self._cache[domain] = (verdict, self._clock() + ttl)
            self._cache.move_to_end(domain)
            while len(self._cache) > self._max_entries:
                self._cache.popitem(last=False)
