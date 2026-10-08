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

# What the fake chain says each holder of the winning token is owed: 1 apUSD,
# well above the $0.01 claim minimum, so the opt-in is the only thing deciding.
_WON = 1_000_000


def _settings() -> Settings:
    return Settings(_env_file=None, min_claim_micro=10_000, auto_redeem_max_per_pass=20)


def _won_chain(yes_token: str) -> MagicMock:
    """An admin whose chain says YES (index 0) won and every holder asked
    about holds `_WON` of it and none of the loser."""
    admin = MagicMock()
    admin.payout_vector.return_value = (1, [1, 0])
    admin.ctf_balances.side_effect = lambda _address, token_ids: [
        _WON if str(t) == yes_token else 0 for t in token_ids
    ]
    return admin


@pytest.fixture()
def db_with_a_won_position(monkeypatch):
    """Builder for `(db, admin)`: a RESOLVED market with one holder of the
    winning token.

    `db` is a real DbSession carrying the market, a user, and the trade row
    `list_participant_api_keys_for_market` needs to find them. `admin` is a
    MagicMock whose `payout_vector` says YES won and whose `ctf_balances`
    reports `_WON` of the winning token and zero of the loser.

    `PositionService.redeem` is stubbed to a no-op: the on-chain send/sign
    plumbing it drives is already covered in tests/onchain, and a bare
    MagicMock admin can't survive a real `eth_account.sign_transaction` call.
    This keeps the test focused on what `auto_redeem_resolved_markets` itself
    decides -- whether to call redeem at all -- not on the transaction below it.
    """

    def _build(*, auto_redeem: bool):
        db = fresh_test_db()
        yes_token = str(int.from_bytes(secrets.token_bytes(8), "big"))
        no_token = str(int.from_bytes(secrets.token_bytes(8), "big"))

        with db.write() as conn:
            row = conn.execute(
                "INSERT INTO markets (CONDITION_ID, QUESTION, SLUG, DESCRIPTION, "
                "ERC1155_TOKENS, START_DATE, MARKET_STATE, RESOLVED_OUTCOME) "
                "VALUES (%s, 'Already won?', %s, 'd', %s, 100, 'RESOLVED', 0) "
                "RETURNING MARKET_ID",
                (
                    f"0x{secrets.token_hex(32)}",
                    f"already-won-{secrets.token_hex(4)}",
                    json.dumps([[yes_token, "YES"], [no_token, "NO"]]),
                ),
            ).fetchone()
            market_id = row["MARKET_ID"]

            user_id, _acct, api_key = TableWrite.create_user(
                conn,
                email=f"redeem-{secrets.token_hex(4)}@example.com",
                password_hash="x",
                handle=None,
            )
            TableWrite.set_auto_redeem(conn, user_id, auto_redeem)

            conn.execute(
                "INSERT INTO trades (TRADE_ID, ASSET_ID, TAKER_API_KEY, "
                "MAKER_API_KEY, STATUS, MATCH_TIME) VALUES (%s, %s, %s, %s, "
                "'MATCHED', 1)",
                (secrets.token_hex(8), yes_token, api_key, api_key),
            )

        assert market_id  # sanity: the market row was created

        monkeypatch.setattr(
            PositionService,
            "redeem",
            lambda self, user, market_id, *, payout_vector=None: None,
        )

        return db, _won_chain(yes_token)

    return _build


def test_an_account_that_has_not_opted_in_is_skipped(db_with_a_won_position):
    db, admin = db_with_a_won_position(auto_redeem=False)
    assert auto_redeem_resolved_markets(db, admin, _settings()) == 0


def test_an_account_that_opted_in_is_claimed_for(db_with_a_won_position):
    db, admin = db_with_a_won_position(auto_redeem=True)
    assert auto_redeem_resolved_markets(db, admin, _settings()) == 1


def test_the_pass_pays_gas_only_through_the_sponsor(
    db_with_a_won_position, monkeypatch
):
    """Flipped 2026-10-08 (gasless claims). Until then auto-redeem never cost
    the admin anything and a holder without gas simply was not paid. Now every
    claim it makes is topped up -- by `UserGasSponsor`, sized to that one
    transaction, and only for a holder owed at least the minimum. The pass
    itself still sends nothing: no grant of its own, and the sponsor it builds
    gets the pass's settings, where the kill switch and the top-up ceiling
    live."""
    built: list[Settings] = []

    class _RecordingSponsor:
        def __init__(self, db, onchain, settings):
            built.append(settings)
            self.min_claim_micro = settings.min_claim_micro

    monkeypatch.setattr(gas_sponsor, "UserGasSponsor", _RecordingSponsor)
    db, admin = db_with_a_won_position(auto_redeem=True)
    settings = _settings()

    assert auto_redeem_resolved_markets(db, admin, settings) == 1
    assert built == [settings]
    assert not admin.fund_gas.called
    assert not admin.send_as_user.called


def test_the_bot_is_claimed_for_while_the_opted_out_human_beside_it_is_not(
    monkeypatch,
):
    """C1: the opt-in gate must discriminate, not skip everyone alike.

    Both holders sit in the *same* resolved market, both hold the winning
    token, and neither has AUTO_REDEEM_ENABLED set. The bot (e.g. the
    liquidity mirror, which is a maker on essentially every trade) has no
    one to ask for consent, so it is claimed for regardless. The human is
    opted out and stays skipped. A test that only ever builds one of these
    accounts cannot show the gate discriminates between them -- both must be
    present in the same pass.
    """
    db = fresh_test_db()
    yes_token = str(int.from_bytes(secrets.token_bytes(8), "big"))
    no_token = str(int.from_bytes(secrets.token_bytes(8), "big"))

    with db.write() as conn:
        row = conn.execute(
            "INSERT INTO markets (CONDITION_ID, QUESTION, SLUG, DESCRIPTION, "
            "ERC1155_TOKENS, START_DATE, MARKET_STATE, RESOLVED_OUTCOME) "
            "VALUES (%s, 'Already won?', %s, 'd', %s, 100, 'RESOLVED', 0) "
            "RETURNING MARKET_ID",
            (
                f"0x{secrets.token_hex(32)}",
                f"already-won-{secrets.token_hex(4)}",
                json.dumps([[yes_token, "YES"], [no_token, "NO"]]),
            ),
        ).fetchone()
        market_id = row["MARKET_ID"]

        bot_user_id, bot_acct, bot_api_key = TableWrite.create_user(
            conn,
            email=f"bot-{secrets.token_hex(4)}@example.com",
            password_hash="x",
            handle=None,
        )
        TableWrite.set_auto_redeem(conn, bot_user_id, False)
        TableWrite.mark_user_as_bot(conn, bot_api_key)

        human_user_id, _acct, human_api_key = TableWrite.create_user(
            conn,
            email=f"human-{secrets.token_hex(4)}@example.com",
            password_hash="x",
            handle=None,
        )
        TableWrite.set_auto_redeem(conn, human_user_id, False)

        # Bot as maker, human as taker on the winning token -- the shape the
        # mirror actually appears in: a counterparty on the other side of
        # (essentially) every trade.
        conn.execute(
            "INSERT INTO trades (TRADE_ID, ASSET_ID, TAKER_API_KEY, "
            "MAKER_API_KEY, STATUS, MATCH_TIME) VALUES (%s, %s, %s, %s, "
            "'MATCHED', 1)",
            (secrets.token_hex(8), yes_token, human_api_key, bot_api_key),
        )

    admin = _won_chain(yes_token)

    redeemed_for: list[str] = []
    monkeypatch.setattr(
        PositionService,
        "redeem",
        lambda self, user, market_id, *, payout_vector=None: redeemed_for.append(
            user.user_id
        ),
    )

    count = auto_redeem_resolved_markets(db, admin, _settings())

    assert count == 1
    assert redeemed_for == [bot_user_id]
    assert human_user_id not in redeemed_for
