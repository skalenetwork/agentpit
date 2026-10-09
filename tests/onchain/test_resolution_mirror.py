"""Polymarket-driven resolution mirror.

Local markets resolve when the upstream Polymarket market resolves. The
admin (which is the local oracle for every locally-prepared condition)
calls `reportPayouts` on the local CTF, then we flip the local row to
RESOLVED. Users redeem locally.
"""

import secrets

import pytest

from agentpit.polymarket.polymarket_sync import UpstreamMarket, parse
from tests.chain_fakes import gamma_row


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


def _fake_pm_market(question_suffix: str) -> UpstreamMarket:
    m = parse(gamma_row(question=f"Resolution mirror {question_suffix}?"))
    assert isinstance(m, UpstreamMarket)
    return m


@pytest.mark.parametrize(
    ("payouts", "winner"), [((1, 0), "Yes"), ((0, 1), "No"), ((1, 1), None)]
)
def test_pay_out_reports_the_payouts_once_and_resolves_the_row(payouts, winner):
    from agentpit.datastructures.market_state import MarketState
    from agentpit.db.table_read import TableRead
    from agentpit.services.market_service import create_markets, pay_out

    admin, db, contracts = _build_admin_and_db()
    market = create_markets(db, admin, [_fake_pm_market(secrets.token_hex(4))])[0]

    pay_out(db, admin, {market.market_id: payouts})
    pay_out(db, admin, {market.market_id: payouts})

    cond_bytes = bytes.fromhex(market.condition_id.value[2:])
    ctf = contracts.ctf
    assert ctf.functions.payoutDenominator(cond_bytes).call() == sum(payouts)
    assert ctf.functions.payoutNumerators(cond_bytes, 0).call() == payouts[0]
    assert ctf.functions.payoutNumerators(cond_bytes, 1).call() == payouts[1]
    with db.read() as conn:
        row = TableRead.read_market(conn, market.market_id)
    assert row is not None
    assert row.market_state == MarketState.RESOLVED
    assert row.payouts == payouts
    assert row.winner == winner
    assert row.resolved_at is not None
