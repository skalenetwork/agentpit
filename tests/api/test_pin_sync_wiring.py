from contextlib import contextmanager

import agentpit.api.app as app_mod


class _FakeDb:
    """DbSession stub whose write()/read() both yield a sentinel connection."""

    def write(self):
        @contextmanager
        def _cm():
            yield "CONN"

        return _cm()

    def read(self):
        @contextmanager
        def _cm():
            yield "CONN"

        return _cm()


class _FakeSettings:
    pinned_series = [("btc-updown-5m", 300)]


def test_run_pin_sync_syncs_then_returns_current_window_ids(monkeypatch):
    calls = {"sync": 0, "ids": 0, "sync_conn": None, "ids_conn": None}

    def fake_sync(conn, admin, pinned, now):
        calls["sync"] += 1
        calls["sync_conn"] = conn
        assert pinned == [("btc-updown-5m", 300)]
        assert isinstance(now, int)
        return ["m1", "m2", "m3"]

    def fake_ids(conn, pinned, now):
        calls["ids"] += 1
        calls["ids_conn"] = conn
        return [10, 20]

    monkeypatch.setattr(app_mod, "sync_pinned_series", fake_sync)
    monkeypatch.setattr(app_mod, "current_window_market_ids", fake_ids)

    ids = app_mod._run_pin_sync(
        _FakeDb(), admin="ADMIN", settings=_FakeSettings()  # type: ignore[arg-type]
    )

    # Returns the current-window ids (for the immediate fill), not a count.
    assert ids == [10, 20]
    assert calls["sync"] == 1 and calls["ids"] == 1
    assert calls["sync_conn"] == "CONN"  # sync ran inside db.write()
    assert calls["ids_conn"] == "CONN"  # lookup ran inside db.read()


class _FakeResolveSettings(_FakeSettings):
    auto_redeem_enabled = True


def test_run_pin_resolve_returns_only_resolved_and_redeemed(monkeypatch):
    """Regression: the rotating-scan change gave the *general* resolution cycle
    a cursor and a third return value, and the same edit landed on this
    function, which has no cursor and never scans. `NameError: scan_after`
    then fired on every pass -- invisible to the suite, because the loop logs
    and swallows it and nothing else calls this. The caller unpacks two."""
    seen = {}

    def fake_ids(conn, pinned, now):
        seen["ids_conn"] = conn
        return [7]

    def fake_mirror(conn, admin, *, now, market_ids):
        seen["market_ids"] = market_ids
        return 1

    monkeypatch.setattr(app_mod, "ended_unresolved_window_ids", fake_ids)
    def fake_redeem(db, admin, settings):
        seen["redeem_settings"] = settings
        return 2

    monkeypatch.setattr(app_mod, "mirror_polymarket_resolutions", fake_mirror)
    monkeypatch.setattr(app_mod, "auto_redeem_resolved_markets", fake_redeem)

    settings = _FakeResolveSettings()
    result = app_mod._run_pin_resolve(
        _FakeDb(), admin="ADMIN", settings=settings  # type: ignore[arg-type]
    )

    assert result == (1, 2)
    assert seen["redeem_settings"] is settings
    # Scoped to the just-ended windows, not a whole-table scan.
    assert seen["market_ids"] == {7}
    assert seen["ids_conn"] == "CONN"
