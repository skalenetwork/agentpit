"""Polymarket-driven resolution mirror.

Local markets resolve when the upstream Polymarket market resolves. The
admin (which is the local oracle for every locally-prepared condition)
calls `reportPayouts` on the local CTF, then we flip the local row to
RESOLVED. Users redeem locally.
"""

import secrets

import pytest


def _build_admin_and_db():
    from agentpit.config import Settings
    from agentpit.onchain.admin import OnchainAdmin
    from agentpit.onchain.contracts import Contracts
    from agentpit.onchain.deployment import Deployment
    from agentpit.onchain.web3_client import Web3Client
    from tests.db_helpers import fresh_test_db

    s = Settings()
    d = Deployment.load(s.deployment_path)
    w = Web3Client(s, d)
    c = Contracts(w.web3, d)
    admin = OnchainAdmin(w, c)
    db = fresh_test_db()
    return admin, db, c


def test_admin_report_payouts_writes_payout_numerators_on_local_ctf():
    """Bare-metal: prepare a fresh condition, admin reports payouts, the
    CTF's payoutNumerators / payoutDenominator reflect it.
    """
    from eth_utils.crypto import keccak
    from web3 import Web3

    from agentpit.services.market_service import prepare_market_on_chain

    admin, _db, contracts = _build_admin_and_db()

    question = f"Admin payout test {secrets.token_hex(4)}?"
    # prepare_market_on_chain handles prepareCondition + registerToken.
    condition_id, _tokens = prepare_market_on_chain(admin, question, ["YES", "NO"])
    question_id = keccak(text=question)

    # YES wins.
    admin.report_payouts(question_id, [1, 0])

    cond_bytes = bytes.fromhex(condition_id.value[2:])
    ctf = contracts.ctf
    assert ctf.functions.payoutDenominator(cond_bytes).call() == 1
    assert ctf.functions.payoutNumerators(cond_bytes, 0).call() == 1
    assert ctf.functions.payoutNumerators(cond_bytes, 1).call() == 0


def _fake_pm_market(question_suffix: str) -> dict:
    return {
        "id": int(secrets.token_hex(4), 16),
        "conditionId": "0x" + secrets.token_hex(32),
        "question": f"Resolution mirror {question_suffix}?",
        "description": "Resolution mirror test",
        "slug": f"resolution-mirror-{question_suffix}",
        "startDate": "2026-01-01T00:00:00Z",
        "endDate": "2027-01-01T00:00:00Z",
        "active": True,
        "closed": False,
        "tokens": [
            {"token_id": str(int(secrets.token_hex(8), 16)), "outcome": "Yes"},
            {"token_id": str(int(secrets.token_hex(8), 16)), "outcome": "No"},
        ],
    }


def _resolved_pm_response(pm: dict, *, winner_index: int) -> dict:
    """Shape mimics Gamma `/markets?id=...` response for a settled binary market."""
    out = dict(pm)
    out["closed"] = True
    out["umaResolutionStatus"] = "settled"
    out["tokens"] = [
        dict(t, winner=(idx == winner_index)) for idx, t in enumerate(pm["tokens"])
    ]
    return out


def test_mirror_resolves_local_market_when_upstream_settled():
    from agentpit.datastructures.market_state import MarketState
    from agentpit.db.table_read import TableRead
    from agentpit.polymarket.polymarket_sync import (
        create_polymarket_markets_if_needed,
        mirror_polymarket_resolutions,
    )

    admin, db, contracts = _build_admin_and_db()
    pm = _fake_pm_market(secrets.token_hex(4))

    with db.write() as conn:
        created = create_polymarket_markets_if_needed(conn, [pm], admin)
    assert len(created) == 1
    market = created[0]

    fake_response = _resolved_pm_response(pm, winner_index=0)

    def fetcher(polymarket_condition_id: str) -> dict:
        assert polymarket_condition_id == pm["conditionId"]
        return fake_response

    with db.write() as conn:
        mirror_polymarket_resolutions(conn, admin, fetcher=fetcher, now=9_999_999_999)
        row = TableRead.read_market(conn, market.market_id)
    assert row is not None
    assert row.market_state == MarketState.RESOLVED
    assert row.resolved_outcome == 0
    assert row.resolved_at is not None

    cond_bytes = bytes.fromhex(market.condition_id.value[2:])
    ctf = contracts.ctf
    assert ctf.functions.payoutDenominator(cond_bytes).call() == 1
    assert ctf.functions.payoutNumerators(cond_bytes, 0).call() == 1
    assert ctf.functions.payoutNumerators(cond_bytes, 1).call() == 0


def test_mirror_skips_when_upstream_not_resolved():
    from agentpit.datastructures.market_state import MarketState
    from agentpit.db.table_read import TableRead
    from agentpit.polymarket.polymarket_sync import (
        create_polymarket_markets_if_needed,
        mirror_polymarket_resolutions,
    )

    admin, db, _ = _build_admin_and_db()
    pm = _fake_pm_market(secrets.token_hex(4))

    with db.write() as conn:
        created = create_polymarket_markets_if_needed(conn, [pm], admin)
    market = created[0]

    def fetcher(_polymarket_condition_id: str) -> dict:
        # Still open upstream.
        return dict(pm, closed=False)

    with db.write() as conn:
        mirror_polymarket_resolutions(conn, admin, fetcher=fetcher, now=9_999_999_999)
        row = TableRead.read_market(conn, market.market_id)
    assert row is not None
    assert row.market_state == MarketState.ACTIVE
    assert row.resolved_outcome is None


def test_mirror_takes_the_slug_from_the_clob_response():
    from agentpit.db.table_read import TableRead
    from agentpit.polymarket.polymarket_sync import (
        create_polymarket_markets_if_needed,
        mirror_polymarket_resolutions,
    )

    admin, db, _ = _build_admin_and_db()
    pm = _fake_pm_market(secrets.token_hex(4))
    with db.write() as conn:
        market = create_polymarket_markets_if_needed(conn, [pm], admin)[0]

    def fetcher(_polymarket_condition_id: str) -> dict:
        return dict(pm, closed=False, market_slug="renamed-upstream")

    with db.write() as conn:
        mirror_polymarket_resolutions(conn, admin, fetcher=fetcher, now=9_999_999_999)
        row = TableRead.read_market(conn, market.market_id)
    assert row is not None
    assert row.slug == "renamed-upstream"
    assert row.resolved_at is None


def test_mirror_is_idempotent_on_already_resolved_market():
    from agentpit.datastructures.market_state import MarketState
    from agentpit.db.table_read import TableRead
    from agentpit.polymarket.polymarket_sync import (
        create_polymarket_markets_if_needed,
        mirror_polymarket_resolutions,
    )

    admin, db, _ = _build_admin_and_db()
    pm = _fake_pm_market(secrets.token_hex(4))
    with db.write() as conn:
        created = create_polymarket_markets_if_needed(conn, [pm], admin)
    market = created[0]

    fake = _resolved_pm_response(pm, winner_index=1)

    def fetcher(_polymarket_condition_id: str) -> dict:
        return fake

    with db.write() as conn:
        mirror_polymarket_resolutions(conn, admin, fetcher=fetcher, now=9_999_999_999)
        # Second pass must not raise (e.g. ConditionAlreadyResolved on CTF)
        # and must leave state unchanged.
        mirror_polymarket_resolutions(conn, admin, fetcher=fetcher, now=9_999_999_999)
        row = TableRead.read_market(conn, market.market_id)
    assert row is not None
    assert row.market_state == MarketState.RESOLVED
    assert row.resolved_outcome == 1
