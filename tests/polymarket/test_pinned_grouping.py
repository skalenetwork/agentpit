"""DB-level: two different windows of one series bind to the SAME agentpit
event (one homepage card). On-chain prepare is faked, so no anvil needed.
"""

import secrets

import agentpit.polymarket.pinned as pinned
import agentpit.polymarket.polymarket_sync as sync
from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.market_state import MarketState
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.polymarket.pinned import (
    current_window_market_ids,
    current_window_slug,
)
from tests.chain_fakes import as_batch
from tests.db_helpers import fresh_test_conn


def test_current_window_market_ids_finds_live_window():
    """The just-synced live window's market id is returned (matched by slug),
    so the pin loop can fill liquidity onto it immediately; a series with no
    synced window contributes nothing."""
    conn = fresh_test_conn()
    now = 1781193601
    slug = current_window_slug("btc-updown-5m", 300, now)  # btc-updown-5m-1781193600
    req = CreateMarketRequest(
        question="BTC Up or Down window",
        description="d",
        erc1155_tokens=[("u", "Up"), ("d", "Down")],
        slug=slug,
        condition_id=ConditionId("0x" + secrets.token_hex(32)),
        state=MarketState.ACTIVE,
    )
    market = TableWrite.create_market(conn, req, is_polygon_market=False)

    assert current_window_market_ids(conn, [("btc-updown-5m", 300)], now) == [
        market.market_id
    ]
    # A pinned series with no synced current window yields nothing.
    assert current_window_market_ids(conn, [("eth-updown-5m", 300)], now) == []
    conn.close()


def test_ended_unresolved_window_ids_filters_by_state_and_horizon():
    """Only recently-ended, not-yet-resolved windows of the series are returned
    — the set the fast resolve/redeem loop checks against upstream."""
    conn = fresh_test_conn()
    now = 1781193600

    def _mk(slug: str, end_date: int, state: str) -> int:
        req = CreateMarketRequest(
            question="w", description="d",
            erc1155_tokens=[("u", "Up"), ("d", "Down")], slug=slug,
            condition_id=ConditionId("0x" + secrets.token_hex(32)),
            state=MarketState.ACTIVE,
        )
        m = TableWrite.create_market(conn, req, is_polygon_market=False)
        conn.execute(
            "UPDATE markets SET END_DATE=%s, MARKET_STATE=%s WHERE MARKET_ID=%s",
            (end_date, state, m.market_id),
        )
        return m.market_id

    ended = _mk("btc-updown-5m-100", now - 60, "ACTIVE")       # recently ended -> in
    _mk("btc-updown-5m-200", now - 60, "RESOLVED")             # resolved -> out
    _mk("btc-updown-5m-300", now + 60, "ACTIVE")              # not ended yet -> out
    _mk("btc-updown-5m-400", now - 100_000, "ACTIVE")         # beyond horizon -> out
    _mk("eth-updown-5m-100", now - 60, "ACTIVE")              # other series -> out

    ids = pinned.ended_unresolved_window_ids(conn, [("btc-updown-5m", 300)], now)
    assert ids == [ended]
    conn.close()


def _window_event(window_ts: int) -> dict:
    """Two distinct windows of the SAME series (id 10684) — unique market
    id/conditionId/question per window so they don't collide on dedup."""
    nonce = secrets.token_hex(4)
    return {
        "id": f"win-{window_ts}",
        "slug": f"btc-updown-5m-{window_ts}",
        "title": f"Bitcoin Up or Down - {window_ts}",
        "series": [
            {"id": "10684", "slug": "btc-up-or-down-5m", "title": "BTC Up or Down 5m"}
        ],
        "markets": [
            {
                "id": int(secrets.token_hex(4), 16),
                "conditionId": "0x" + secrets.token_hex(32),
                "question": f"Bitcoin Up or Down - {window_ts}-{nonce}",
                "description": "d",
                "slug": f"btc-updown-5m-{window_ts}",
                "active": True,
                "closed": False,
                "startDate": "2026-06-11T16:00:00Z",
                "endDate": "2026-06-11T16:05:00Z",
                "clobTokenIds": '["111","222"]',
                "outcomes": '["Up","Down"]',
            }
        ],
    }


def test_two_windows_of_one_series_share_event(monkeypatch):
    conn = fresh_test_conn()

    # Fake on-chain prepare: deterministic local condition/token ids, no anvil.
    def fake_prepare(admin, question, labels):
        cid = ConditionId("0x" + secrets.token_hex(32))
        toks = [
            (str(int(secrets.token_hex(8), 16)), labels[0]),
            (str(int(secrets.token_hex(8), 16)), labels[1]),
        ]
        return cid, toks

    monkeypatch.setattr(sync, "prepare_markets_on_chain", as_batch(fake_prepare))

    events = iter([_window_event(1781193600), _window_event(1781193900)])
    monkeypatch.setattr(pinned, "fetch_event_by_slug", lambda slug: next(events))

    first = pinned.sync_pinned_series(
        conn, admin=None, pinned=[("btc-updown-5m", 300)], now=1781193601
    )
    second = pinned.sync_pinned_series(
        conn, admin=None, pinned=[("btc-updown-5m", 300)], now=1781193901
    )

    assert len(first) == 1 and len(second) == 1

    m1 = TableRead.read_market(conn, first[0].market_id)
    m2 = TableRead.read_market(conn, second[0].market_id)
    assert m1 is not None and m2 is not None
    assert m1.event_id is not None
    assert m1.event_id == m2.event_id  # one card for both windows

    event = TableRead.get_event_by_slug(conn, "btc-up-or-down-5m")
    assert event is not None
    assert event.polymarket_event_id == "10684"

    conn.close()


def test_resyncing_same_window_dedups_and_keeps_grouping(monkeypatch):
    """The pin loop re-fetches the SAME live window every cycle until the
    boundary rolls. The second sync must hit the cheap path: no new market
    (deduped by polymarket_id), and the event grouping persists.
    """
    conn = fresh_test_conn()

    prepare_calls = {"n": 0}

    def fake_prepare(admin, question, labels):
        prepare_calls["n"] += 1
        cid = ConditionId("0x" + secrets.token_hex(32))
        toks = [
            (str(int(secrets.token_hex(8), 16)), labels[0]),
            (str(int(secrets.token_hex(8), 16)), labels[1]),
        ]
        return cid, toks

    monkeypatch.setattr(sync, "prepare_markets_on_chain", as_batch(fake_prepare))

    # Same window event returned on every fetch (same polymarket_id).
    event = _window_event(1781193600)
    monkeypatch.setattr(pinned, "fetch_event_by_slug", lambda slug: event)

    first = pinned.sync_pinned_series(
        conn, admin=None, pinned=[("btc-updown-5m", 300)], now=1781193601
    )
    second = pinned.sync_pinned_series(
        conn, admin=None, pinned=[("btc-updown-5m", 300)], now=1781193602
    )

    assert len(first) == 1  # created on the first cycle
    assert second == []  # cheap path: deduped, nothing new created
    assert prepare_calls["n"] == 1  # on-chain prepare ran only once

    # Grouping still intact after the cheap-path re-bind.
    m1 = TableRead.read_market(conn, first[0].market_id)
    assert m1 is not None and m1.event_id is not None
    event_row = TableRead.get_event_by_slug(conn, "btc-up-or-down-5m")
    assert event_row is not None
    assert event_row.event_id == m1.event_id
    assert event_row.polymarket_event_id == "10684"

    conn.close()
