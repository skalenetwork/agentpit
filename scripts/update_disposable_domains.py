#!/usr/bin/env python
"""Refresh the vendored temporary-email blocklist from upstream, by hand.

`agentpit/auth/disposable_email.py` reads `agentpit/auth/data/disposable_domains.txt`
and nothing else: the app never fetches a list at runtime, so a deploy always
runs against the exact file that was reviewed in the diff. This script is how
that file gets updated. It is not imported by the app and nothing schedules it.

It resolves upstream's current `main` commit, downloads the list from that
commit rather than from the moving branch, and writes it under a three-line
header naming the source, the commit and the licence -- so the file always says
exactly what it is a copy of.

Usage:
    .venv/bin/python scripts/update_disposable_domains.py

Then read the diff before committing it: a list that suddenly gains a large
provider is the thing to catch. `disposable_allowlist.txt` is where an alias
service that a list wrongly names is kept allowed.
"""
import re
import sys
import urllib.request
from pathlib import Path

REPO = "disposable-email-domains/disposable-email-domains"
LIST_NAME = "disposable_email_blocklist.conf"
LICENCE = "CC0 1.0 Universal (public domain dedication)"
TARGET = (
    Path(__file__).resolve().parent.parent
    / "agentpit" / "auth" / "data" / "disposable_domains.txt"
)

# What a domain line looks like. Anything else (an HTML error page, a rate-limit
# message) must stop the run before it overwrites a good file.
_DOMAIN_LINE = re.compile(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?\Z")
# Upstream lists thousands; far fewer means a truncated or wrong download.
_MIN_DOMAINS = 1000


def _get(url: str, accept: str | None = None) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "agentpit-update"})
    if accept:
        request.add_header("Accept", accept)
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def latest_commit() -> str:
    """The commit `main` points at, as a bare 40-character SHA."""
    sha = _get(
        f"https://api.github.com/repos/{REPO}/commits/main",
        accept="application/vnd.github.sha",
    ).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError(f"expected a commit SHA from GitHub, got {sha[:80]!r}")
    return sha


def render(sha: str, body: str) -> str:
    """The vendored file's text: the header, then upstream's list unchanged."""
    domains = [line for line in body.splitlines() if line.strip()]
    bad = [line for line in domains if not _DOMAIN_LINE.match(line)]
    if bad:
        raise ValueError(f"not a domain list: {len(bad)} odd lines, first {bad[0][:80]!r}")
    if len(domains) < _MIN_DOMAINS:
        raise ValueError(f"only {len(domains)} domains; expected at least {_MIN_DOMAINS}")
    header = (
        f"# Source: https://github.com/{REPO}/blob/main/{LIST_NAME}\n"
        f"# Commit: {sha}\n"
        f"# Licence: {LICENCE}\n"
    )
    return header + body.rstrip("\n") + "\n"


def main() -> int:
    sha = latest_commit()
    body = _get(f"https://raw.githubusercontent.com/{REPO}/{sha}/{LIST_NAME}")
    text = render(sha, body)
    TARGET.write_text(text, encoding="utf-8")
    print(f"wrote {TARGET} from {sha}: {len(text.splitlines()) - 3} domains")
    return 0


if __name__ == "__main__":
    sys.exit(main())
