"""Auto-redeem claims only for accounts that have it switched on, and for bots.

A redeem is settlement rather than a decision, which is the case for doing it
automatically. The wallet is still the account's, though, so an account that
switched the Settings toggle off is never claimed for.
"""

from __future__ import annotations

import json
import secrets
from unittest.mock import MagicMock

import pytest

from agentpit.config import Settings
from agentpit.db.table_write import TableWrite
from agentpit.polymarket.polymarket_sync import auto_redeem_resolved_markets
from agentpit.services import gas_sponsor
from agentpit.services.position_service import PositionService
from tests.db_helpers import fresh_test_db

# What the fake chain says a holder of the winning token is owed: 1 apUSD, well
# above the $0.01 claim minimum, so the opt-in is the only thing deciding.
_WON = 1_000_000


def _settings() -> Settings:
    return Settings(_env_file=None, min_claim_micro=10_000, auto_redeem_max_per_pass=20)


def _won_chain(yes_token: str) -> MagicMock:
    """An admin whose chain says YES won; every holder holds `_WON` of it."""
    admin = MagicMock()
    admin.payout_vector.return_value = (1, [1, 0])
    admin.ctf_balances.side_effect = lambda _address, token_ids: [
        _WON if str(t) == yes_token else 0 for t in token_ids
    ]
    return admin


def _seed(*labels: str, auto_redeem: bool = False):
    """A RESOLVED market whose YES won, one account per label (all with the
    toggle set to `auto_redeem`), and a trade on it that the first account takes
    and the last makes (the row participants are found by). Returns
    `(db, yes_token, [(user_id, api_key), ...])`."""
    db = fresh_test_db()
    yes, no = (str(int.from_bytes(secrets.token_bytes(8), "big")) for _ in range(2))
    with db.write() as conn:
        row = conn.execute(
            "INSERT INTO markets (CONDITION_ID, QUESTION, SLUG, DESCRIPTION, "
            "ERC1155_TOKENS, START_DATE, MARKET_STATE, RESOLVED_OUTCOME) "
            "VALUES (%s, 'Already won?', %s, 'd', %s, 100, 'RESOLVED', 0) "
            "RETURNING MARKET_ID",
            (
                f"0x{secrets.token_hex(32)}",
                f"already-won-{secrets.token_hex(4)}",
                json.dumps([[yes, "YES"], [no, "NO"]]),
            ),
        ).fetchone()
        assert row["MARKET_ID"]  # sanity: the market row was created
        users = []
        for label in labels:
            user_id, _acct, api_key = TableWrite.create_user(
                conn,
                email=f"{label}-{secrets.token_hex(4)}@example.com",
                password_hash="x",
                handle=None,
            )
            TableWrite.set_auto_redeem(conn, user_id, auto_redeem)
            users.append((user_id, api_key))
        conn.execute(
            "INSERT INTO trades (TRADE_ID, ASSET_ID, TAKER_API_KEY, MAKER_API_KEY, "
            "STATUS, MATCH_TIME) VALUES (%s, %s, %s, %s, 'MATCHED', 1)",
            (secrets.token_hex(8), yes, users[0][1], users[-1][1]),
        )
    return db, yes, users


@pytest.fixture(autouse=True)
def redeemed_for(monkeypatch) -> list[str]:
    """Stub `redeem` (the on-chain plumbing is covered in tests/onchain, and a bare
    MagicMock admin can't survive a real signature) and record who it ran for."""
    users: list[str] = []
    monkeypatch.setattr(
        PositionService,
        "redeem",
        lambda self, user, market_id, *, payout_vector=None: users.append(user.user_id),
    )
    return users


@pytest.mark.parametrize(
    ("auto_redeem", "claimed"), [(False, 0), (True, 1)], ids=["opted-out", "opted-in"]
)
def test_only_an_account_that_opted_in_is_claimed_for(auto_redeem, claimed):
    db, yes_token, _ = _seed("redeem", auto_redeem=auto_redeem)
    assert (
        auto_redeem_resolved_markets(db, _won_chain(yes_token), _settings()) == claimed
    )


def test_the_pass_pays_gas_only_through_the_sponsor(monkeypatch):
    """Gasless claims (2026-10-08): the pass itself sends nothing, and the sponsor
    it builds gets its settings, where the kill switch and top-up ceiling live."""
    built: list[Settings] = []

    class _RecordingSponsor:
        def __init__(self, db, onchain, settings):
            built.append(settings)
            self.min_claim_micro = settings.min_claim_micro

    monkeypatch.setattr(gas_sponsor, "UserGasSponsor", _RecordingSponsor)
    db, yes_token, _ = _seed("redeem", auto_redeem=True)
    admin, settings = _won_chain(yes_token), _settings()

    assert auto_redeem_resolved_markets(db, admin, settings) == 1
    assert built == [settings]
    assert not admin.fund_gas.called
    assert not admin.send_as_user.called


def test_the_bot_is_claimed_for_while_the_opted_out_human_beside_it_is_not(
    redeemed_for,
):
    """C1: the gate must discriminate, so both sit in one market and one pass, both
    holding the winning token, neither opted in. The bot (e.g. the liquidity mirror,
    a maker on nearly every trade) has no one to ask for consent: it is claimed for."""
    db, yes_token, ((human_id, _), (bot_id, bot_key)) = _seed("human", "bot")
    with db.write() as conn:
        TableWrite.mark_user_as_bot(conn, bot_key)

    count = auto_redeem_resolved_markets(db, _won_chain(yes_token), _settings())

    assert count == 1
    assert redeemed_for == [bot_id]
    assert human_id not in redeemed_for
