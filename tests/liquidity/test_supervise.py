# tests/liquidity/test_supervise.py
import asyncio
import logging

import pytest

from agentpit.liquidity.supervise import supervise


async def _until(pred, timeout=1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.005)


async def _stop(task):
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_child_that_returns_is_started_again():
    starts = []

    async def child():
        starts.append(1)

    sup = asyncio.create_task(supervise("returner", child, restart_delay=0.01))
    await _until(lambda: len(starts) >= 3)
    await _stop(sup)


async def test_a_child_that_raises_is_logged_at_error_and_started_again(caplog):
    starts = []

    async def child():
        starts.append(1)
        raise RuntimeError("boom")

    caplog.set_level(logging.ERROR, logger="agentpit.liquidity.supervise")
    sup = asyncio.create_task(supervise("raiser", child, restart_delay=0.01))
    await _until(lambda: len(starts) >= 2)
    await _stop(sup)

    crashes = [r for r in caplog.records
               if r.levelno == logging.ERROR and "raiser" in r.getMessage()]
    assert crashes and crashes[0].exc_info is not None
    assert isinstance(crashes[0].exc_info[1], RuntimeError)


async def test_a_hung_child_is_cancelled_and_replaced_once_it_reports_stale(caplog):
    starts = []
    cancelled = []
    stale = {"now": False}

    async def child():
        starts.append(1)
        try:
            await asyncio.Event().wait()     # hangs forever, never raises
        except asyncio.CancelledError:
            cancelled.append(1)
            raise

    caplog.set_level(logging.ERROR, logger="agentpit.liquidity.supervise")
    sup = asyncio.create_task(supervise(
        "hanger", child, is_stale=lambda: stale["now"],
        check_interval=0.01, restart_delay=0.01))
    await _until(lambda: len(starts) == 1)
    await asyncio.sleep(0.05)
    assert len(starts) == 1, "a healthy hang is left alone"

    stale["now"] = True
    await _until(lambda: len(starts) == 2)
    stale["now"] = False
    assert cancelled == [1], "the hung child is cancelled before its replacement"
    assert any(r.levelno == logging.ERROR and "hanger" in r.getMessage()
               for r in caplog.records)
    await asyncio.sleep(0.05)
    assert len(starts) == 2, "a fresh child that is not stale keeps running"
    await _stop(sup)


async def test_cancelling_the_supervisor_cancels_the_child_and_leaves_no_tasks():
    child_task = {}

    async def child():
        child_task["t"] = asyncio.current_task()
        await asyncio.Event().wait()

    before = asyncio.all_tasks()
    sup = asyncio.create_task(supervise(
        "shutdown", child, is_stale=lambda: False, check_interval=0.01))
    await _until(lambda: "t" in child_task)
    await _stop(sup)

    assert child_task["t"].cancelled()
    assert asyncio.all_tasks() == before


async def test_a_child_that_will_not_stop_is_reported_and_never_run_twice(caplog):
    # A task that keeps running after it was cancelled cannot be replaced
    # safely: starting a second copy of the feed would deliver every book
    # event twice. So the supervisor waits for it — but loudly. The incident
    # this module exists for went unseen for sixteen days; waiting silently
    # would repeat exactly that.
    import time

    starts = []
    stale = {"v": False}

    async def child():
        starts.append(1)
        try:
            await asyncio.Event().wait()
        finally:
            end = time.monotonic() + 0.15
            while time.monotonic() < end:
                try:
                    await asyncio.sleep(end - time.monotonic())
                except asyncio.CancelledError:
                    pass

    caplog.set_level(logging.ERROR)
    sup = asyncio.create_task(supervise(
        "stubborn", child, is_stale=lambda: stale["v"],
        check_interval=0.005, restart_delay=0.001, cancel_grace=0.02))
    await _until(lambda: starts == [1])
    stale["v"] = True
    await _until(lambda: "still running" in caplog.text)
    assert starts == [1], "no second copy while the first is still alive"
    stale["v"] = False
    await _until(lambda: len(starts) == 2, timeout=1.0)
    await _stop(sup)
