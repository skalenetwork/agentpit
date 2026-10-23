"""The operator token: refused at startup when guessable, never matched empty.

Every /admin and market-lifecycle route trusts X-Admin-Token alone, and anyone
on the internet can send the header.
"""

import pytest
from fastapi import HTTPException

from agentpit.api.app import _check_admin_token
from agentpit.api.deps import require_admin_token
from agentpit.config import Settings
from agentpit.onchain.deployment import ANVIL_CHAIN_ID

SKALE_BASE = 1187947933
STRONG = "a" * 64


@pytest.mark.parametrize(
    "token", ["", "   ", "CHANGE_ME", "dev-admin-token", "x" * 31], ids=repr
)
def test_a_guessable_token_stops_startup_on_a_real_chain(token):
    with pytest.raises(RuntimeError, match="AGENTPIT_ADMIN_TOKEN"):
        _check_admin_token(Settings(AGENTPIT_ADMIN_TOKEN=token), SKALE_BASE)


def test_a_strong_token_starts_on_a_real_chain():
    _check_admin_token(Settings(AGENTPIT_ADMIN_TOKEN=STRONG), SKALE_BASE)


def test_a_local_anvil_starts_with_the_default_and_warns(caplog):
    _check_admin_token(Settings(AGENTPIT_ADMIN_TOKEN="dev-admin-token"), ANVIL_CHAIN_ID)
    assert "admin_token" in caplog.text


def _gate(configured: str, header: str | None) -> None:
    require_admin_token(Settings(AGENTPIT_ADMIN_TOKEN=configured), header)


@pytest.mark.parametrize("header", ["", None])
def test_an_empty_configured_token_matches_nothing(header):
    with pytest.raises(HTTPException) as raised:
        _gate("", header)
    assert raised.value.status_code == 401


def test_a_non_ascii_header_is_a_401_not_a_crash():
    with pytest.raises(HTTPException) as raised:
        _gate(STRONG, "tökén")
    assert raised.value.status_code == 401


def test_the_configured_token_passes():
    assert _gate(STRONG, STRONG) is None
