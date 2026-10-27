"""The tick values every account once the mirror is ready, thins and reads
the chain's transfers whether or not it is, and each step runs although
another failed.

A unit test of thinning stays green even when nothing calls it, so this
asserts the wiring.
"""
import asyncio
import logging
from types import SimpleNamespace

from agentpit.api.app import _leaderboard_loop, _run_leaderboard_tick
from agentpit.liquidity import feed
from agentpit.services import leaderboard_service


class _SpyService:
    def __init__(self, failing: str | None = None):
        self.calls: list[str] = []
        self._failing = failing

    def _step(self, name: str, result=None):
        self.calls.append(name)
        if name == self._failing:
            raise RuntimeError(f"{name} broke")
        return result

    def take_snapshot(self, only: set[str] | None = None) -> int:
        return self._step("snapshot" if only is None else "touched", 3)

    def thin_snapshots(self, now: int) -> int:
        return self._step("thin", 7)

    def exclude_peer_transfers(self) -> None:
        self._step("transfers")


def test_the_tick_writes_then_thins_then_scans_transfers():
    service = _SpyService()
    assert _run_leaderboard_tick(service, False) == (3, 7)
    assert service.calls == ["snapshot", "thin", "transfers"]


def test_a_tick_before_the_mirror_is_ready_only_thins_and_scans(monkeypatch):
    monkeypatch.setattr(feed, "ready", lambda engine_enabled: not engine_enabled)
    service = _SpyService()
    assert _run_leaderboard_tick(service, True) == (0, 7)
    assert service.calls == ["thin", "transfers"]


def _errors(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]


def test_a_failed_valuation_does_not_stop_the_thinning_or_the_scan(caplog):
    service = _SpyService(failing="snapshot")
    assert _run_leaderboard_tick(service, False) == (0, 7)
    assert service.calls == ["snapshot", "thin", "transfers"]
    assert any("valuation" in message for message in _errors(caplog))


def test_a_failed_thinning_does_not_stop_the_scan(caplog):
    service = _SpyService(failing="thin")
    assert _run_leaderboard_tick(service, False) == (3, 0)
    assert service.calls == ["snapshot", "thin", "transfers"]
    assert any("thinning" in message for message in _errors(caplog))


def test_a_failed_scan_says_no_competition_can_turn_final(caplog):
    service = _SpyService(failing="transfers")
    assert _run_leaderboard_tick(service, False) == (3, 7)
    assert service.calls == ["snapshot", "thin", "transfers"]
    assert any("competitions cannot turn final" in message for message in _errors(caplog))


def test_a_failed_touched_account_valuation_does_not_skip_the_tick(monkeypatch, caplog):
    """The loop values the accounts touched since the last pass before it
    ticks: its failure must not cost the iteration its scan."""
    monkeypatch.setattr("agentpit.api.app._LEADERBOARD_TICK_SECONDS", 3600)  # one iteration only
    service = _SpyService(failing="touched")
    settings = SimpleNamespace(leaderboard_interval_seconds=3600, liquidity_engine_enabled=False)

    async def one_iteration():
        leaderboard_service.touch("0xabc")
        task = asyncio.create_task(_leaderboard_loop(service, settings))  # type: ignore[arg-type]
        for _ in range(100):
            if "transfers" in service.calls:
                break
            await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    with caplog.at_level(logging.ERROR, logger="agentpit.api.app"):
        asyncio.run(one_iteration())

    assert service.calls == ["touched", "snapshot", "thin", "transfers"]
    assert any("touched accounts" in message for message in _errors(caplog))
