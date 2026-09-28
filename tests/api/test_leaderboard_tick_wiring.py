"""The tick thins as well as writes, in that order and at the same instant.

A unit test of thinning stays green even when nothing calls it, so this
asserts the wiring.
"""
from agentpit.api.app import _run_leaderboard_tick


class _SpyService:
    def __init__(self):
        self.calls: list[tuple[str, int]] = []

    def take_snapshot(self, now: int) -> int:
        self.calls.append(("snapshot", now))
        return 3

    def thin_snapshots(self, now: int) -> int:
        self.calls.append(("thin", now))
        return 7


def test_the_tick_writes_then_thins():
    service = _SpyService()
    assert _run_leaderboard_tick(service) == (3, 7)
    (first, written_at), (second, thinned_at) = service.calls
    assert (first, second) == ("snapshot", "thin")
    assert written_at == thinned_at
