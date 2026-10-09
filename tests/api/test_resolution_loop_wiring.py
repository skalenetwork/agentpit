from contextlib import contextmanager
from types import SimpleNamespace

import pytest

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
    assert reconciled == []  # the pass settles pending rows itself, first
    assert isinstance(calls["now"], int)
    # The pass gets the cycle's settings: its claim minimum and cap live there.
    assert calls["settings"] is settings


class _FakeDb:
    @contextmanager
    def write(self):
        yield "CONN"


@pytest.mark.parametrize("fails", [False, True], ids=["reconciles", "reconcile-fails"])
def test_a_disabled_redeem_cycle_reconciles_on_its_own(monkeypatch, caplog, fails):
    """With the switch off the redeeming pass never reconciles, so a claim that mined
    unseen would stay pending: the loop settles it itself, under the redeem lock."""
    _no_scan(monkeypatch)
    monkeypatch.setattr(
        app_mod,
        "mirror_polymarket_resolutions",
        lambda conn, admin, *, now, candidates=None: 1 if candidates is None else 0,
    )
    redeem_calls, seen = [], []
    monkeypatch.setattr(
        app_mod,
        "auto_redeem_resolved_markets",
        lambda db, admin, settings: redeem_calls.append(1) or 0,
    )

    def reconcile(db, admin):
        seen.append((db.__class__.__name__, admin, app_mod._redeem_lock.locked()))
        if fails:
            raise RuntimeError("database gone")
        return 0

    monkeypatch.setattr(app_mod, "reconcile_pending_user_txs", reconcile)

    disabled = SimpleNamespace(auto_redeem_enabled=False, resolution_scan_batch=200)
    resolved, redeemed, _scan = app_mod._run_resolution_cycle(
        _FakeDb(), admin="ADMIN", settings=disabled  # type: ignore[arg-type]
    )

    assert (resolved, redeemed) == (1, 0)
    assert redeem_calls == []
    assert seen == [("_FakeDb", "ADMIN", True)]
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == int(fails) and all(r.exc_info is not None for r in errors)
    assert not app_mod._redeem_lock.locked()  # not left held for the other loop
