"""We stop transacting from someone's wallet unless they asked us to.

A redeem is settlement rather than a decision, which is the case for doing it
automatically. It is outweighed by the wallet being theirs — the same wallet we
now hand them the key to.
"""

from __future__ import annotations

import json
import secrets
from unittest.mock import MagicMock

import pytest

from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.services.market_service import redeem_resolved_markets
from agentpit.services.position_service import PositionService
from tests.db_helpers import fresh_test_db


@pytest.fixture()
def db_with_a_won_position(monkeypatch):
    """Builder for `(db, admin)`: a RESOLVED market with one holder of the
    winning token.

    `db` is a real DbSession carrying the market, a user, and the trade row
    `list_participant_api_keys_for_market` needs to find them. `admin` is a
    MagicMock whose `ctf_balances` reports a positive balance for the winning
    token and zero for the loser.

    `PositionService.redeem` is stubbed to a no-op: the on-chain send/sign
    plumbing it drives is already covered in tests/onchain, and a bare
    MagicMock admin can't survive a real `eth_account.sign_transaction` call.
    This keeps the test focused on what `redeem_resolved_markets` itself
    decides -- whether to call redeem at all -- not on the transaction below it.
    """

    def _build(*, auto_redeem: bool):
        db = fresh_test_db()
        yes_token = str(int.from_bytes(secrets.token_bytes(8), "big"))
        no_token = str(int.from_bytes(secrets.token_bytes(8), "big"))

        with db.write() as conn:
            row = conn.execute(
                "INSERT INTO markets (CONDITION_ID, QUESTION_ID, QUESTION, SLUG, DESCRIPTION, "
                "ERC1155_TOKENS, START_DATE, MARKET_STATE, PAYOUTS) "
                "VALUES (%s, %s, 'Already won?', %s, 'd', %s, 100, 'RESOLVED', '{1,0}') "
                "RETURNING MARKET_ID",
                (
                    f"0x{secrets.token_hex(32)}",
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

        admin = MagicMock()
        admin.ctf_balances.side_effect = lambda _address, tokens: [
            100 if str(t) == yes_token else 0 for t in tokens
        ]
        monkeypatch.setattr(
            PositionService, "redeem", lambda self, user, market_id: None
        )

        return db, admin, market_id

    return _build


def test_an_account_that_has_not_opted_in_is_skipped(db_with_a_won_position):
    db, admin, _ = db_with_a_won_position(auto_redeem=False)
    assert redeem_resolved_markets(db, admin, 10) == 0


def test_an_account_that_opted_in_is_claimed_for(db_with_a_won_position):
    db, admin, _ = db_with_a_won_position(auto_redeem=True)
    assert redeem_resolved_markets(db, admin, 10) == 1


def test_no_gas_is_ever_sent(db_with_a_won_position):
    """Task 1 removed the top-up; this is the behavioural proof, not a grep."""
    db, admin, _ = db_with_a_won_position(auto_redeem=True)
    redeem_resolved_markets(db, admin, 10)
    assert not admin.fund_gas.called


def test_each_resolved_market_is_redeemed_once(db_with_a_won_position):
    db, admin, market_id = db_with_a_won_position(auto_redeem=False)
    assert redeem_resolved_markets(db, admin, 10) == 0
    with db.read() as conn:
        assert TableRead.read_market(conn, market_id).fully_redeemed is True
        assert TableRead.list_resolved_unredeemed_markets(conn, 10) == []


def test_a_failed_redeem_leaves_the_market_for_a_later_pass(
    db_with_a_won_position, monkeypatch
):
    db, admin, market_id = db_with_a_won_position(auto_redeem=True)

    def out_of_gas(self, user, market_id):
        raise RuntimeError("insufficient funds for gas")

    monkeypatch.setattr(PositionService, "redeem", out_of_gas)
    assert redeem_resolved_markets(db, admin, 10) == 0
    with db.read() as conn:
        assert TableRead.list_resolved_unredeemed_markets(conn, 10)[0].market_id == market_id
    monkeypatch.setattr(PositionService, "redeem", lambda self, user, market_id: None)
    assert redeem_resolved_markets(db, admin, 10) == 1
    with db.read() as conn:
        assert TableRead.list_resolved_unredeemed_markets(conn, 10) == []


def test_the_budget_caps_the_markets_per_pass(db_with_a_won_position):
    _, _, first = db_with_a_won_position(auto_redeem=True)
    db, admin, _ = db_with_a_won_position(auto_redeem=True)
    redeem_resolved_markets(db, admin, 1)
    with db.read() as conn:
        assert [m.market_id for m in TableRead.list_resolved_unredeemed_markets(conn, 10)] == [first]


def test_the_house_is_no_longer_claimed_for(monkeypatch):
    """The house only buys at fill time and never sends a transaction after
    provisioning, so a bot holder with auto-redeem off is skipped like the
    opted-out human beside it.
    """
    db = fresh_test_db()
    yes_token = str(int.from_bytes(secrets.token_bytes(8), "big"))
    no_token = str(int.from_bytes(secrets.token_bytes(8), "big"))

    with db.write() as conn:
        row = conn.execute(
            "INSERT INTO markets (CONDITION_ID, QUESTION_ID, QUESTION, SLUG, DESCRIPTION, "
            "ERC1155_TOKENS, START_DATE, MARKET_STATE, PAYOUTS) "
            "VALUES (%s, %s, 'Already won?', %s, 'd', %s, 100, 'RESOLVED', '{1,0}') "
            "RETURNING MARKET_ID",
            (
                f"0x{secrets.token_hex(32)}",
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

        conn.execute(
            "INSERT INTO trades (TRADE_ID, ASSET_ID, TAKER_API_KEY, "
            "MAKER_API_KEY, STATUS, MATCH_TIME) VALUES (%s, %s, %s, %s, "
            "'MATCHED', 1)",
            (secrets.token_hex(8), yes_token, human_api_key, bot_api_key),
        )

    admin = MagicMock()
    admin.ctf_balances.side_effect = lambda _address, tokens: [
        100 if str(t) == yes_token else 0 for t in tokens
    ]

    redeemed_for: list[str] = []
    monkeypatch.setattr(
        PositionService,
        "redeem",
        lambda self, user, market_id: redeemed_for.append(user.user_id),
    )

    assert redeem_resolved_markets(db, admin, 10) == 0
    assert redeemed_for == []
