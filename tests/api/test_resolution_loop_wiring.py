import agentpit.api.app as app_mod


def _no_scan(monkeypatch):
    """The rotating scan needs a real table; these tests fake the connection,
    so stub the reader out. Its own coverage lives in
    tests/polymarket/test_mirror_candidates.py."""
    monkeypatch.setattr(app_mod, "list_scan_candidates", lambda conn, **kw: [])


def test_run_resolution_cycle_resolves_then_redeems(monkeypatch):
    _no_scan(monkeypatch)
    calls = {"mirror": 0, "redeem": 0, "now": None, "settings": None}

    class FakeSettings:
        auto_redeem_enabled = True
        resolution_scan_batch = 200

    class FakeDb:
        def write(self):
            from contextlib import contextmanager

            @contextmanager
            def _cm():
                yield "CONN"

            return _cm()

    def fake_mirror(conn, admin, *, now, candidates=None):
        calls["mirror"] += 1
        calls["now"] = now
        assert conn == "CONN"
        return 2 if candidates is None else 0

    def fake_redeem(db, admin, settings):
        calls["redeem"] += 1
        calls["settings"] = settings
        return 3

    monkeypatch.setattr(app_mod, "mirror_polymarket_resolutions", fake_mirror)
    monkeypatch.setattr(app_mod, "auto_redeem_resolved_markets", fake_redeem)

    settings = FakeSettings()
    resolved, redeemed, _scan = app_mod._run_resolution_cycle(
        FakeDb(), admin="ADMIN", settings=settings  # type: ignore[arg-type]
    )

    assert (resolved, redeemed) == (2, 3)
    assert calls["mirror"] == 1 and calls["redeem"] == 1
    assert isinstance(calls["now"], int)
    # The pass gets the cycle's settings: its claim minimum and per-pass cap
    # live there.
    assert calls["settings"] is settings


def test_run_resolution_cycle_skips_redeem_when_disabled(monkeypatch):
    _no_scan(monkeypatch)

    class FakeSettings:
        auto_redeem_enabled = False
        resolution_scan_batch = 200

    class FakeDb:
        def write(self):
            from contextlib import contextmanager

            @contextmanager
            def _cm():
                yield "CONN"

            return _cm()

    monkeypatch.setattr(
        app_mod,
        "mirror_polymarket_resolutions",
        lambda conn, admin, *, now, candidates=None: 1 if candidates is None else 0,
    )
    called = {"redeem": False}

    def fake_redeem(db, admin, settings):
        called["redeem"] = True
        return 0

    monkeypatch.setattr(app_mod, "auto_redeem_resolved_markets", fake_redeem)

    resolved, redeemed, _scan = app_mod._run_resolution_cycle(
        FakeDb(), admin="ADMIN", settings=FakeSettings()  # type: ignore[arg-type]
    )
    assert (resolved, redeemed) == (1, 0)
    assert called["redeem"] is False
