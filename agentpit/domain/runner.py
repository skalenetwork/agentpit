"""What an agent runs on, from the OAuth client it signed in with.

`app` is the WorkOS OAuth client name and `host` the hostname of its default
redirect URI; no app means the agent trades by API key.
"""
from typing import Literal

from pydantic import BaseModel

RunnerSlug = Literal[
    "claude", "claude-code", "chatgpt", "codex", "cursor", "grok", "poke", "openclaw",
    "clawbits", "hermes", "gemini", "goose", "vscode", "meta", "api", "unknown",
]

LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
OWN_DOMAINS = ("claude.ai", "chatgpt.com", "cursor.com", "grok.com", "poke.com", "x.ai", "clawbits.ai")
APPS: dict[str, tuple[RunnerSlug, str]] = {
    "Claude": ("claude", "Claude"),
    "Claude Code": ("claude-code", "Claude Code"),
    "ChatGPT": ("chatgpt", "ChatGPT"),
    "Codex": ("codex", "Codex"),
    "Grok": ("grok", "Grok"),
    "Poke": ("poke", "Poke"),
    "OpenClaw MCP": ("openclaw", "OpenClaw"),
    "Hermes Agent": ("hermes", "Hermes Agent"),
    "Gemini": ("gemini", "Gemini"),
    "goose": ("goose", "goose"),
    "Visual Studio Code": ("vscode", "Visual Studio Code"),
    "Muse": ("meta", "Muse"),
}


class Runner(BaseModel):
    slug: RunnerSlug
    label: str
    host: str | None


def runner_for(app: str | None, host: str | None) -> Runner:
    """Grok Bot signs in through Cursor's MCP OAuth client, so Cursor is only
    Cursor when it redirects to the machine it runs on."""
    slug: RunnerSlug
    if app is None:
        slug, label = "api", "API"
    elif app == "Cursor":
        slug, label = ("cursor", "Cursor") if host in LOOPBACK else ("grok", "Grok Bot")
    elif app == "OpenClaw MCP" and host and host.endswith("clawbits.ai"):
        slug, label = "clawbits", "Clawbits"
    else:
        slug, label = APPS.get(app, ("unknown", app))
    if host is None or any(host == d or host.endswith(f".{d}") for d in OWN_DOMAINS):
        shown = None
    else:
        shown = "Self-hosted" if host in LOOPBACK else host
    return Runner(slug=slug, label=label, host=shown)
