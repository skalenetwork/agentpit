# agentpit/liquidity/supervise.py
"""Keep a long-running background coroutine alive.

Lifespan loops used to be started with a bare `asyncio.create_task(...)`. On
2026-09-02 07:05 UTC the mirror's feed task stopped (died or hung — nothing
recorded which) and no market created after that was ever mirrored: 543
active markets sat on empty books for over two weeks with nobody noticing.
`supervise` restarts a child that ends, and — given an `is_stale` probe —
replaces one that is still running but no longer doing its job, which a plain
restart-on-exit can never catch.
"""
import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

log = logging.getLogger(__name__)


async def supervise(
    name: str,
    factory: Callable[[], Coroutine[Any, Any, None]],
    *,
    is_stale: Callable[[], bool] | None = None,
    check_interval: float = 30.0,
    restart_delay: float = 5.0,
    cancel_grace: float = 30.0,
) -> None:
    """Run `factory()` forever, restarting it when it ends or goes stale.

    Returns only by cancellation, which is passed on to the child and awaited
    before `CancelledError` is re-raised, so shutdown leaves no task behind.
    """
    while True:
        child = asyncio.create_task(factory(), name=name)
        try:
            await _watch(name, child, is_stale, check_interval, restart_delay,
                         cancel_grace)
        except asyncio.CancelledError:
            await _cancel(child, cancel_grace)
            raise
        await asyncio.sleep(restart_delay)


async def _watch(
    name: str,
    child: asyncio.Task,
    is_stale: Callable[[], bool] | None,
    check_interval: float,
    restart_delay: float,
    cancel_grace: float,
) -> None:
    """Return once `child` has ended or been cancelled for going stale."""
    if is_stale is None:
        # Nothing to probe: the only event is the child ending.
        await asyncio.wait({child})
        _report_exit(name, child, restart_delay)
        return
    while True:
        done, _ = await asyncio.wait({child}, timeout=check_interval)
        if done:
            _report_exit(name, child, restart_delay)
            return
        try:
            stale = is_stale()
        except Exception:
            # A broken probe must not take the supervisor down with it; the
            # child keeps running and the next check tries again.
            log.exception("%s health check failed", name)
            continue
        if stale:
            log.error("%s is stale — cancelling and restarting in %.1fs",
                      name, restart_delay)
            await _cancel(child, cancel_grace)
            return


def _report_exit(name: str, child: asyncio.Task, restart_delay: float) -> None:
    if child.cancelled():
        log.error("%s was cancelled — restarting in %.1fs", name, restart_delay)
    elif (exc := child.exception()) is not None:
        log.error("%s crashed — restarting in %.1fs", name, restart_delay,
                  exc_info=exc)
    else:
        log.error("%s returned — restarting in %.1fs", name, restart_delay)


async def _cancel(child: asyncio.Task, grace: float) -> None:
    """Cancel `child` and wait until it has actually stopped.

    It is never abandoned to start a replacement alongside it: two copies of
    the feed would each apply every book event, and a stale copy that later
    wakes up would write into books the live one now owns. What must not
    happen is waiting silently — so every `grace` seconds the wait says so.

    `asyncio.wait` never raises the child's outcome into this coroutine, so a
    cancellation aimed at the supervisor itself propagates untouched.
    """
    child.cancel()
    waited = 0.0
    while True:
        done, _ = await asyncio.wait({child}, timeout=grace)
        if done:
            break
        waited += grace
        log.error("%s is still running %.0fs after it was cancelled — it "
                  "cannot be replaced until it stops", child.get_name(), waited)
    if not child.cancelled() and (exc := child.exception()) is not None:
        log.error("%s failed while being cancelled", child.get_name(),
                  exc_info=exc)
