"""Tests for the liquidity-engine read helpers added in Phase 5b Task 4."""

from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.market_state import MarketState
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from tests.db_helpers import fresh_test_conn


def _hex32(seed: str) -> str:
    """Return a 32-byte hex condition_id derived from a short label."""
    payload = seed.encode().hex().ljust(64, "0")[:64]
    return "0x" + payload


def _make_market(conn, *, question: str, cond_id: str, polymarket_condition_id=None,
                 state: MarketState = MarketState.DRAFT):
    """Insert a market via CreateMarketRequest, then force state/polymarket_condition_id."""
    request = CreateMarketRequest(
        question=question,
        description=f"desc for {question}",
        erc1155_tokens=[(f"{cond_id}-yes", "Yes"), (f"{cond_id}-no", "No")],
        slug=question.lower().replace(" ", "-").replace("?", ""),
        condition_id=ConditionId(cond_id),
        state=MarketState.DRAFT,
        polymarket_condition_id=polymarket_condition_id,
    )
    market = TableWrite.create_market(conn, request, is_polygon_market=False)
    # Force desired state via direct UPDATE (create_market sets state from request,
    # but we use DRAFT first so we can control each case explicitly below).
    if state != MarketState.DRAFT or polymarket_condition_id is not None:
        conn.execute(
            "UPDATE markets SET MARKET_STATE = %s, POLYMARKET_CONDITION_ID = %s "
            "WHERE MARKET_ID = %s",
            (state.value, polymarket_condition_id, market.market_id),
        )
    return market


# ---------------------------------------------------------------------------
# list_active_synced_markets
# ---------------------------------------------------------------------------


def test_list_active_synced_markets_filters():
    conn = fresh_test_conn()

    # (a) ACTIVE + has polymarket_condition_id -> expected
    m_a = _make_market(
        conn,
        question="Active synced?",
        cond_id=_hex32("active-synced"),
        polymarket_condition_id="0xdeadbeef" + "0" * 56,
        state=MarketState.ACTIVE,
    )

    # (b) ACTIVE but NO polymarket_condition_id -> excluded
    _make_market(
        conn,
        question="Active unsynced?",
        cond_id=_hex32("active-unsynced"),
        polymarket_condition_id=None,
        state=MarketState.ACTIVE,
    )

    # (c) RESOLVED + has polymarket_condition_id -> excluded
    _make_market(
        conn,
        question="Resolved synced?",
        cond_id=_hex32("resolved-synced"),
        polymarket_condition_id="0xcafebabe" + "0" * 56,
        state=MarketState.RESOLVED,
    )

    got = TableRead.list_active_synced_markets(conn)
    assert len(got) == 1
    assert got[0].market_id == m_a.market_id
    assert got[0].market_state == MarketState.ACTIVE
    assert got[0].polymarket_condition_id is not None


def test_list_active_synced_markets_empty_when_none_qualify():
    conn = fresh_test_conn()
    # Only a DRAFT market — should not appear
    _make_market(
        conn,
        question="Draft?",
        cond_id=_hex32("draft-market"),
        polymarket_condition_id="0xaabbccdd" + "0" * 56,
        state=MarketState.DRAFT,
    )
    assert TableRead.list_active_synced_markets(conn) == []


def test_list_active_synced_markets_ordered_by_market_id():
    # Market id is the TIE-BREAK, not the sort key: neither market here belongs
    # to an event with a captured volume, so both fall to the bottom of the
    # volume ranking and this is what decides between them. The test above it
    # covers the ranking itself.
    conn = fresh_test_conn()
    pcid_a = "0x" + "aa" * 32
    pcid_b = "0x" + "bb" * 32
    m1 = _make_market(
        conn,
        question="First?",
        cond_id=_hex32("first"),
        polymarket_condition_id=pcid_a,
        state=MarketState.ACTIVE,
    )
    m2 = _make_market(
        conn,
        question="Second?",
        cond_id=_hex32("second"),
        polymarket_condition_id=pcid_b,
        state=MarketState.ACTIVE,
    )
    got = TableRead.list_active_synced_markets(conn)
    assert [m.market_id for m in got] == sorted([m1.market_id, m2.market_id])


def test_active_markets_come_back_busiest_first():
    """Volume beats id, and the mirror deepens books in exactly this order.

    The quiet market is created FIRST, so it holds the lower market id. If the
    ranking ever regresses to `ORDER BY MARKET_ID` this test fails, which is
    the point — the regression is invisible in production until somebody
    notices the popular markets filled in last.
    """
    conn = fresh_test_conn()
    quiet = TableWrite.upsert_event(conn, slug="quiet-ev", title="Quiet")
    busy = TableWrite.upsert_event(conn, slug="busy-ev", title="Busy")
    TableWrite.update_event_volume(conn, quiet.event_id, 10.0)
    TableWrite.update_event_volume(conn, busy.event_id, 9_000_000.0)

    low_id = _make_market(
        conn,
        question="Quiet one?",
        cond_id=_hex32("quiet"),
        polymarket_condition_id="0x" + "cc" * 32,
        state=MarketState.ACTIVE,
    )
    high_id = _make_market(
        conn,
        question="Busy one?",
        cond_id=_hex32("busy"),
        polymarket_condition_id="0x" + "dd" * 32,
        state=MarketState.ACTIVE,
    )
    conn.execute(
        "UPDATE markets SET EVENT_ID = %s WHERE MARKET_ID = %s",
        (quiet.event_id, low_id.market_id),
    )
    conn.execute(
        "UPDATE markets SET EVENT_ID = %s WHERE MARKET_ID = %s",
        (busy.event_id, high_id.market_id),
    )

    got = TableRead.list_active_synced_markets(conn)

    assert [m.market_id for m in got] == [high_id.market_id, low_id.market_id]
    conn.close()
