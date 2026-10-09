from typing import Any

import pytest

from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.market_state import MarketState
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from tests.db_helpers import fresh_test_conn

NOW = 1_800_000_000
WEEK = 7 * 86_400


@pytest.fixture()
def db() -> Any:
    conn = fresh_test_conn()
    yield conn
    conn.close()


def _event(db, slug: str, category: str | None = None) -> int:
    return TableWrite.upsert_event(db, slug=slug, title=slug, category=category).event_id


def _market(db, event_id: int, name: str, state: MarketState, end_date: int = NOW):
    cid = "0x" + name.encode().hex().ljust(64, "0")[:64]
    market = TableWrite.create_market(
        db,
        CreateMarketRequest(
            question=f"{name}?",
            description="d",
            erc1155_tokens=[(f"{cid}-y", "Yes"), (f"{cid}-n", "No")],
            condition_id=ConditionId(cid),
            state=MarketState.ACTIVE if state == MarketState.RESOLVED else state,
            start_date=1,
            end_date=end_date,
            event_id=event_id,
        ),
        is_polygon_market=False,
    )
    if state == MarketState.RESOLVED:
        _settle(db, market.market_id, None)
    return market


def _settle(db, market_id: int, resolved_at: int | None) -> None:
    db.execute(
        "UPDATE markets SET MARKET_STATE = 'RESOLVED', PAYOUTS = '{1,0}', "
        "RESOLVED_AT = %s WHERE MARKET_ID = %s",
        (resolved_at, market_id),
    )


def _board(db, **kw) -> dict[str, list[str]]:
    rows = TableRead.board_events(
        db, settled_since=NOW - WEEK, excluded_categories=kw.get("categories"),
        excluded_tags=kw.get("tags"),
    )
    return {e.slug: [m.question for m in ms] for e, ms in rows}


def test_live_and_recently_settled_events_are_on_the_board(db):
    live = _event(db, "live")
    _market(db, live, "open", MarketState.ACTIVE)
    _market(db, live, "void", MarketState.CANCELLED)
    _market(db, live, "done", MarketState.CLOSED)
    recent = _event(db, "recent")
    _market(db, recent, "recent", MarketState.RESOLVED, end_date=NOW - WEEK + 60)
    old = _event(db, "old")
    _market(db, old, "old", MarketState.RESOLVED, end_date=NOW - WEEK - 60)
    _event(db, "empty")

    assert _board(db) == {"live": ["open?", "done?"], "recent": ["recent?"]}


def test_the_resolution_time_beats_the_end_date(db):
    event = _event(db, "late")
    market = _market(db, event, "late", MarketState.ACTIVE, end_date=NOW - 2 * WEEK)
    _settle(db, market.market_id, NOW - 60)

    assert _board(db) == {"late": ["late?"]}


def test_excluded_categories_and_tags_stay_off_the_board(db):
    _market(db, _event(db, "kept", "Politics"), "kept", MarketState.ACTIVE)
    _market(db, _event(db, "weather", "Weather"), "weather", MarketState.ACTIVE)
    tagged = _market(db, _event(db, "tagged"), "tagged", MarketState.ACTIVE)
    TableWrite.replace_market_tags(
        db, market_id=tagged.market_id, tags=[("temperature", "Temperature")]
    )

    assert _board(db, categories=["weather"], tags=["temperature"]) == {
        "kept": ["kept?"]
    }


def test_tag_slugs_are_the_union_over_an_events_markets(db):
    event = _event(db, "game")
    first = _market(db, event, "first", MarketState.ACTIVE)
    second = _market(db, event, "second", MarketState.ACTIVE)
    TableWrite.replace_market_tags(
        db, market_id=first.market_id, tags=[("sports", "Sports"), ("games", "Games")]
    )
    TableWrite.replace_market_tags(
        db, market_id=second.market_id, tags=[("sports", "Sports"), ("nfl", "NFL")]
    )
    bare = _event(db, "bare")
    _market(db, bare, "bare", MarketState.ACTIVE)

    assert TableRead.tag_slugs_by_event(db, [event, bare]) == {
        event: {"sports", "games", "nfl"}
    }


def test_a_market_context_names_the_only_paid_outcome(db):
    event = _event(db, "context")
    won = _market(db, event, "won", MarketState.ACTIVE)
    split = _market(db, event, "split", MarketState.ACTIVE)
    TableWrite.set_market_state(db, won.market_id, MarketState.ACTIVE, MarketState.RESOLVED, (0, 1))
    db.execute(
        "UPDATE markets SET MARKET_STATE = 'RESOLVED', PAYOUTS = '{1,1}' WHERE MARKET_ID = %s",
        (split.market_id,),
    )

    contexts = TableRead.market_contexts(db, [won.condition_id.value, split.condition_id.value], [])
    assert [contexts[m.condition_id.value].winner for m in (won, split)] == ["No", None]


def test_a_market_context_gives_a_games_kickoff(db):
    event = _event(db, "kickoff")
    db.execute("UPDATE events SET START_TIME = %s WHERE EVENT_ID = %s", (NOW, event))
    game = _market(db, event, "game", MarketState.ACTIVE)
    other = _market(db, event, "other", MarketState.ACTIVE)
    TableWrite.replace_market_tags(db, market_id=game.market_id, tags=[("games", "Games")])

    contexts = TableRead.market_contexts(
        db, [game.condition_id.value, other.condition_id.value], [game.erc1155_tokens[0][0]]
    )
    assert [
        (contexts[m.condition_id.value].kickoff, contexts[m.condition_id.value].trading)
        for m in (game, other)
    ] == [(NOW, True), (None, None)]


def test_resolve_stamps_the_resolution_time(db):
    event = _event(db, "stamp")
    market = _market(db, event, "by-id", MarketState.ACTIVE)

    TableWrite.set_market_state(db, market.market_id, MarketState.ACTIVE, MarketState.RESOLVED, (0, 1))

    now = int(db.execute("SELECT EXTRACT(EPOCH FROM now())::BIGINT AS T").fetchone()["T"])
    row = TableRead.read_market(db, market.market_id)
    assert row is not None and row.resolved_at is not None
    assert abs(row.resolved_at - now) <= 5
