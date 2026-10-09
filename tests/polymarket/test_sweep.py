import json

import httpx

from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.market_state import MarketState
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.polymarket.polymarket_sync import Clob, clob_market
from agentpit.services.market_service import sweep
from tests.db_helpers import fresh_test_db


def _cid(name: str) -> str:
    return "0x" + name.encode().hex().ljust(64, "0")


def _market(db, name: str, state: MarketState = MarketState.ACTIVE, event: str | None = None) -> int:
    with db.write() as conn:
        event_id = (
            TableWrite.upsert_event(conn, slug=event, title=event, polymarket_event_id=event).event_id
            if event
            else None
        )
        return TableWrite.create_market(
            conn,
            CreateMarketRequest(
                question=f"{name}?",
                description="d",
                erc1155_tokens=[(f"{name}-y", "Yes"), (f"{name}-n", "No")],
                slug=name,
                condition_id=ConditionId(_cid("local-" + name)),
                polymarket_condition_id=_cid(name),
                state=state,
                start_date=1,
                event_id=event_id,
            ),
            is_polygon_market=False,
        ).market_id


def _row(name: str, **fields) -> dict:
    n = int.from_bytes(name.encode(), "big")
    row = {
        "id": "1",
        "conditionId": _cid(name),
        "question": f"{name}?",
        "slug": name,
        "description": "d",
        "clobTokenIds": json.dumps([f"{n}1", f"{n}2"]),
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.4", "0.6"]',
        "closed": False,
        "acceptingOrders": True,
        "version": "v1",
        "startDate": "2026-01-01T00:00:00Z",
        "endDate": "2026-12-31T00:00:00Z",
        "image": f"https://img/{name}.png",
        "groupItemTitle": name.title(),
        "oneDayPriceChange": 0.01,
        "tags": [],
        "events": [],
    }
    return {k: v for k, v in (row | fields).items() if v is not ...}


def _event(slug: str, **fields) -> dict:
    return {
        "id": slug,
        "slug": slug,
        "title": slug.title(),
        "image": f"https://img/{slug}.png",
        "startDate": "2026-01-01T00:00:00Z",
        "endDate": "2026-12-31T00:00:00Z",
        "volume24hr": 10.0,
        "volume": 100.0,
        "liquidity": 5.0,
        "competitive": 0.5,
    } | fields


class Upstream:
    def __init__(self, rows: list[dict], clob: dict[str, httpx.Response] | None = None):
        self.rows = {r["conditionId"]: r for r in rows}
        self.clob = clob or {}
        self.gamma: list[tuple[list[str], dict[str, str]]] = []
        self.clob_asked: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "clob.polymarket.com":
            cid = request.url.path.rsplit("/", 1)[1]
            self.clob_asked.append(cid)
            return self.clob.get(cid, httpx.Response(404, json={"error": "market not found"}))
        ids = request.url.params.get_list("condition_ids")
        params = {k: v for k, v in request.url.params.items() if k != "condition_ids"}
        self.gamma.append((ids, params))
        closed = params["closed"] == "true"
        tagged = params.get("include_tag") == "true"
        rows = [self.rows[c] if tagged else {**self.rows[c], "tags": None} for c in ids if c in self.rows]
        return httpx.Response(200, json=[r for r in rows if (r.get("closed") is True) == closed])

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def _states(db) -> dict[str, MarketState]:
    with db.read() as conn:
        rows = conn.execute("SELECT SLUG, MARKET_STATE FROM markets").fetchall()
    return {r["SLUG"]: MarketState(r["MARKET_STATE"]) for r in rows}


def _xmin(db, table: str) -> dict[str, str]:
    with db.read() as conn:
        rows = conn.execute(f"SELECT SLUG, xmin::text AS X FROM {table}").fetchall()
    return {r["SLUG"]: r["X"] for r in rows}


def _never(*_):
    raise AssertionError("nothing here resolves")


def test_only_changed_rows_are_written_and_a_missing_field_keeps_what_is_stored():
    db = fresh_test_db()
    _market(db, "moved", event="ev-moved")
    _market(db, "still", event="ev-still")
    upstream = Upstream([
        _row("moved", events=[_event("ev-moved")]),
        _row("still", events=[_event("ev-still")]),
    ])
    sweep(db, upstream.client(), _never)
    markets, events = _xmin(db, "markets"), _xmin(db, "events")

    upstream.rows[_cid("moved")] |= {
        "question": "Moved again?",
        "image": None,
        "events": [_event("ev-moved", volume24hr=99.0, image=None)],
    }
    sweep(db, upstream.client(), _never)

    after_markets, after_events = _xmin(db, "markets"), _xmin(db, "events")
    assert after_markets["still"] == markets["still"] and after_markets["moved"] != markets["moved"]
    assert after_events["ev-still"] == events["ev-still"] and after_events["ev-moved"] != events["ev-moved"]
    with db.read() as conn:
        carried = {c.fields.slug: c for c in TableRead.list_carried(conn)}
    moved_event = carried["moved"].event
    assert moved_event is not None
    assert (moved_event.volume_24hr, moved_event.icon_url) == (99.0, "https://img/ev-moved.png")
    assert (carried["moved"].fields.question, carried["moved"].fields.icon_url) == (
        "Moved again?",
        "https://img/moved.png",
    )
    assert carried["still"].fields.outcome_label == "Still"


def test_a_moved_kickoff_is_refreshed_and_a_missing_one_keeps_what_is_stored():
    db = fresh_test_db()
    _market(db, "game", event="ev-game")
    upstream = Upstream([_row("game", events=[_event("ev-game", startTime="2026-10-10T16:00:00Z")])])

    def kickoff() -> int | None:
        sweep(db, upstream.client(), _never)
        with db.read() as conn:
            (carried,) = TableRead.list_carried(conn)
        assert carried.event is not None
        return carried.event.start_time

    assert kickoff() == 1_791_648_000
    upstream.rows[_cid("game")]["events"] = [_event("ev-game", startTime="2026-10-10T19:30:00Z")]
    assert kickoff() == 1_791_660_600
    upstream.rows[_cid("game")]["events"] = [_event("ev-game")]
    assert kickoff() == 1_791_660_600


def test_event_identity_tags_and_category_follow_upstream_and_none_keeps_what_is_stored():
    db = fresh_test_db()
    _market(db, "game", event="ev-game")
    _market(db, "taken", event="ev-taken")
    tags = [{"slug": "politics", "label": "Politics"}, {"slug": "world", "label": "World"}]
    renamed = _event("ev-renamed", id="ev-game", gameId=7, seriesSlug="nfl-2026")
    upstream = Upstream([_row("game", tags=tags, events=[renamed]), _row("taken", events=[_event("ev-taken")])])

    def game():
        sweep(db, upstream.client(), _never)
        with db.read() as conn:
            c = {c.fields.slug: c for c in TableRead.list_carried(conn)}["game"]
        assert c.event is not None
        return c.tags, (c.event.slug, c.event.game_id, c.event.series_slug, c.event.category)

    event = ("ev-renamed", "7", "nfl-2026", "World")
    assert game() == ({("politics", "Politics"), ("world", "World")}, event)
    written = [_xmin(db, table) for table in ("markets", "events", "market_tags")]
    game()
    assert [_xmin(db, table) for table in ("markets", "events", "market_tags")] == written
    upstream.rows[_cid("game")] |= {"tags": tags[:1], "events": [_event("ev-taken", id="ev-game")]}
    assert game() == ({("politics", "Politics")}, event)
    written = [_xmin(db, table) for table in ("markets", "events", "market_tags")]
    game()
    assert [_xmin(db, table) for table in ("markets", "events", "market_tags")] == written


def test_ids_missing_from_the_open_ask_are_asked_again_with_closed_true():
    db = fresh_test_db()
    _market(db, "open")
    _market(db, "shut")
    upstream = Upstream([_row("open"), _row("shut", closed=True)])

    sweep(db, upstream.client(), _never)

    (first, first_params), (second, second_params) = upstream.gamma
    assert (sorted(first), second) == (sorted([_cid("open"), _cid("shut")]), [_cid("shut")])
    assert (first_params["limit"], first_params["closed"], second_params["closed"]) == ("50", "false", "true")
    assert first_params["_cb"] != second_params["_cb"]
    assert _states(db) == {"open": MarketState.ACTIVE, "shut": MarketState.CLOSED}


def test_an_active_market_closes_only_on_an_explicit_close_or_stop(caplog):
    db = fresh_test_db()
    for name in ("closed", "stopped", "silent", "trading"):
        _market(db, name)
    upstream = Upstream([
        _row("closed", closed=True, acceptingOrders=...),
        _row("stopped", acceptingOrders=False),
        _row("silent", closed=..., acceptingOrders=...),
        _row("trading"),
    ])

    sweep(db, upstream.client(), _never)

    assert _states(db) == {
        "closed": MarketState.CLOSED,
        "stopped": MarketState.CLOSED,
        "silent": MarketState.ACTIVE,
        "trading": MarketState.ACTIVE,
    }
    assert upstream.clob_asked == []
    assert "4 carried, 0 missing upstream" in caplog.text and "2 closed, 0 reopened" in caplog.text


def test_a_closed_market_reopens_only_when_the_clob_accepts_orders_again():
    db = fresh_test_db()
    for name in ("back", "halted", "flaky", "unsure"):
        _market(db, name, MarketState.CLOSED)
    upstream = Upstream(
        [_row("back"), _row("halted"), _row("flaky"), _row("unsure", acceptingOrders=...)],
        clob={
            _cid("back"): httpx.Response(200, json={"c": _cid("back"), "ao": True}),
            _cid("halted"): httpx.Response(200, json={"c": _cid("halted")}),
            _cid("flaky"): httpx.Response(503, text="<html>"),
        },
    )

    sweep(db, upstream.client(), _never)

    assert _states(db) == {
        "back": MarketState.ACTIVE,
        "halted": MarketState.CLOSED,
        "flaky": MarketState.CLOSED,
        "unsure": MarketState.CLOSED,
    }
    assert sorted(upstream.clob_asked) == sorted([_cid("back"), _cid("halted"), _cid("flaky")])


def test_exact_payout_vectors_go_to_the_chain_task_and_anything_else_stays_closed():
    db = fresh_test_db()
    ids = {name: _market(db, name, MarketState.CLOSED) for name in ("split", "yes", "odd", "pending")}
    ids["live"] = _market(db, "live")
    resolved = {"closed": True, "umaResolutionStatus": "resolved"}
    upstream = Upstream([
        _row("split", outcomePrices='["0.5", "0.5"]', **resolved),
        _row("yes", outcomePrices='["1", "0"]', **resolved),
        _row("odd", outcomePrices='["0.9995", "0.0005"]', **resolved),
        _row("pending", outcomePrices='["1", "0"]', closed=True),
        _row("live", outcomePrices='["0", "1"]', **resolved),
    ])
    queued = []

    sweep(db, upstream.client(), lambda *args: queued.append(args))

    assert sorted(queued) == sorted([
        (ids["split"], (1, 1)),
        (ids["yes"], (1, 0)),
        (ids["live"], (0, 1)),
    ])
    assert set(_states(db).values()) == {MarketState.CLOSED}


def test_the_clob_answer_counts_only_when_it_names_the_market():
    cid = _cid("named")
    answers = {
        "open": httpx.Response(200, json={"c": cid, "ao": True}),
        "kickoff": httpx.Response(
            200, json={"c": cid.upper().replace("0X", "0x"), "ao": True, "cbos": True, "gst": "2026-10-07T12:00:00Z"}
        ),
        "closed": httpx.Response(200, json={"c": cid, "cbos": False, "gst": "2026-10-07T12:00:00Z"}),
        "other": httpx.Response(200, json={"c": _cid("other"), "ao": False}),
        "missing": httpx.Response(404, json={"error": "market not found"}),
        "html": httpx.Response(403, text="<html>blocked</html>"),
    }

    def ask(name: str) -> Clob | None:
        transport = httpx.MockTransport(lambda request: answers[name])
        return clob_market(httpx.Client(transport=transport), cid)

    assert ask("open") == Clob(True, None)
    assert ask("kickoff") == Clob(True, 1_791_374_400)
    assert ask("closed") == Clob(False, None)
    assert [ask(n) for n in ("other", "missing", "html")] == [None, None, None]
