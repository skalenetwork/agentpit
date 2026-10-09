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

    reconciled = []
    monkeypatch.setattr(app_mod, "mirror_polymarket_resolutions", fake_mirror)
    monkeypatch.setattr(app_mod, "auto_redeem_resolved_markets", fake_redeem)
    monkeypatch.setattr(
        app_mod, "reconcile_pending_user_txs", lambda db, admin: reconciled.append(1)
    )

    settings = FakeSettings()
    resolved, redeemed, _scan = app_mod._run_resolution_cycle(
        FakeDb(), admin="ADMIN", settings=settings  # type: ignore[arg-type]
    )

    assert (resolved, redeemed) == (2, 3)
    assert calls["mirror"] == 1 and calls["redeem"] == 1
    # The auto-redeem pass settles the pending rows itself, first; a second
    # reconcile beside it would read the same rows twice.
    assert reconciled == []
    assert isinstance(calls["now"], int)
    # The pass gets the cycle's settings: its claim minimum and per-pass cap
    # live there.
    assert calls["settings"] is settings


class _DisabledSettings:
    auto_redeem_enabled = False
    resolution_scan_batch = 200


class _FakeDb:
    def write(self):
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            yield "CONN"

        return _cm()


def _disabled_cycle(monkeypatch, reconcile):
    """One resolution cycle with auto-redeem off; returns what auto-redeem was
    called with (nothing, when the wiring is right)."""
    _no_scan(monkeypatch)
    monkeypatch.setattr(
        app_mod,
        "mirror_polymarket_resolutions",
        lambda conn, admin, *, now, candidates=None: 1 if candidates is None else 0,
    )
    redeem_calls = []
    monkeypatch.setattr(
        app_mod,
        "auto_redeem_resolved_markets",
        lambda db, admin, settings: redeem_calls.append(1) or 0,
    )
    monkeypatch.setattr(app_mod, "reconcile_pending_user_txs", reconcile)
    result = app_mod._run_resolution_cycle(
        _FakeDb(), admin="ADMIN", settings=_DisabledSettings()  # type: ignore[arg-type]
    )
    return result, redeem_calls


def test_run_resolution_cycle_reconciles_on_its_own_when_redeem_disabled(monkeypatch):
    """With the global auto-redeem switch off the pass that reconciles never
    runs, so a claim or split that mined unseen would stay a pending row for
    good. The loop settles them itself, under the lock the redeem pass uses."""
    seen = []

    def reconcile(db, admin):
        seen.append((db.__class__.__name__, admin, app_mod._redeem_lock.locked()))
        return 0

    (resolved, redeemed, _scan), redeem_calls = _disabled_cycle(monkeypatch, reconcile)

    assert (resolved, redeemed) == (1, 0)
    assert redeem_calls == []
    assert seen == [("_FakeDb", "ADMIN", True)]


def test_a_reconcile_that_fails_does_not_break_the_resolution_cycle(
    monkeypatch, caplog
):
    def reconcile(db, admin):
        raise RuntimeError("database gone")

    (resolved, redeemed, _scan), _ = _disabled_cycle(monkeypatch, reconcile)

    assert (resolved, redeemed) == (1, 0)
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1 and errors[0].exc_info is not None
    # And the lock is not left held for the other loop.
    assert not app_mod._redeem_lock.locked()
