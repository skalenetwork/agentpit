"""Polymarket sync mirrors markets onto the LOCAL exchange.

A synced Polymarket market must end up with locally-prepared conditionId
and registered token IDs so trading actually works against our anvil/CTF.
The upstream Polymarket conditionId/token IDs are kept only as informational
linkage via `polymarket_id`.
"""

import secrets

from agentpit.polymarket.polymarket_sync import UpstreamMarket, parse
from tests.chain_fakes import gamma_row


def _fake_pm_market(question_suffix: str) -> UpstreamMarket:
    m = parse(gamma_row(question=f"Sync mirror {question_suffix}?"))
    assert isinstance(m, UpstreamMarket)
    return m


def test_sync_mirrors_market_onto_local_exchange():
    from agentpit.config import Settings
    from agentpit.db.table_read import TableRead
    from agentpit.onchain.admin import OnchainAdmin
    from agentpit.onchain.contracts import Contracts
    from agentpit.onchain.deployment import Deployment
    from agentpit.onchain.web3_client import Web3Client
    from agentpit.services.market_service import create_markets
    from tests.db_helpers import fresh_test_db

    settings = Settings()
    d = Deployment.load(settings.deployment_path)
    w = Web3Client(settings, d)
    c = Contracts(w.web3, d)
    admin = OnchainAdmin(w, c)

    db = fresh_test_db()

    pm = _fake_pm_market(secrets.token_hex(4))
    upstream_yes, upstream_no = pm.tokens

    created = create_markets(db, admin, [pm])
    assert len(created) == 1, "sync should mirror the fake pm market"
    market = created[0]

    # The stored condition_id and token IDs must be the *locally-prepared*
    # ones (not Polymarket's), and both tokens must be registered on the
    # local exchange so matchOrders won't revert with InvalidTokenId.
    local_yes_str, local_yes_label = market.erc1155_tokens[0]
    local_no_str, local_no_label = market.erc1155_tokens[1]
    assert local_yes_label.upper() == "YES"
    assert local_no_label.upper() == "NO"
    assert local_yes_str != upstream_yes, "YES token id must be local, not upstream"
    assert local_no_str != upstream_no, "NO token id must be local, not upstream"

    comp_yes, _ = c.exchange.functions.registry(int(local_yes_str)).call()
    comp_no, _ = c.exchange.functions.registry(int(local_no_str)).call()
    assert comp_yes == int(local_no_str), "YES must point at NO complement"
    assert comp_no == int(local_yes_str), "NO must point at YES complement"

    # Upstream linkage preserved.
    with db.read() as conn:
        row = TableRead.read_market(conn, market.market_id)
    assert row is not None
    assert row.polymarket_id == pm.pm_id

