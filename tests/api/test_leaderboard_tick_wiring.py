"""The tick values every account once the mirror is ready, thins and reads
the chain's transfers whether or not it is.

A unit test of thinning stays green even when nothing calls it, so this
asserts the wiring.
"""
from agentpit.api.app import _run_leaderboard_tick
from agentpit.liquidity import feed


class _SpyService:
    def __init__(self):
        self.calls: list[str] = []

    def take_snapshot(self) -> int:
        self.calls.append("snapshot")
        return 3

    def thin_snapshots(self, now: int) -> int:
        self.calls.append("thin")
        return 7

    def exclude_peer_transfers(self) -> None:
        self.calls.append("transfers")


def test_the_tick_writes_then_thins_then_scans_transfers():
    service = _SpyService()
    assert _run_leaderboard_tick(service, False) == (3, 7)
    assert service.calls == ["snapshot", "thin", "transfers"]


def test_a_tick_before_the_mirror_is_ready_only_thins_and_scans(monkeypatch):
    monkeypatch.setattr(feed, "ready", lambda engine_enabled: not engine_enabled)
    service = _SpyService()
    assert _run_leaderboard_tick(service, True) == (0, 7)
    assert service.calls == ["thin", "transfers"]
