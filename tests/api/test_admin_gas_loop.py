import asyncio
import logging

from agentpit.api.app import _admin_gas_loop, _start_admin_gas_loop
from agentpit.config import Settings


class _Admin:
    def __init__(self, state):
        self.state = state

    def refresh_admin_gas(self):
        return 3 * 10**18, self.state


async def _one_pass(admin):
    task = asyncio.create_task(_admin_gas_loop(admin, Settings(AGENTPIT_ADMIN_GAS_CHECK_INTERVAL_SECONDS=3600)))  # type: ignore[call-arg]
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def test_low_balance_logs_an_error(caplog):
    with caplog.at_level(logging.ERROR, logger="agentpit.api.app"):
        asyncio.run(_one_pass(_Admin("paused")))
    assert any("ADMIN GAS" in r.getMessage() for r in caplog.records)


def test_healthy_balance_logs_nothing(caplog):
    with caplog.at_level(logging.ERROR, logger="agentpit.api.app"):
        asyncio.run(_one_pass(_Admin("ok")))
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING and "Admin gas loop is OFF" in r.getMessage()]


def test_interval_zero_with_a_stop_level_warns_that_the_breaker_is_off(caplog):
    settings = Settings(AGENTPIT_ADMIN_GAS_CHECK_INTERVAL_SECONDS=0, AGENTPIT_ADMIN_GAS_STOP_GAS=5)  # type: ignore[call-arg]
    with caplog.at_level(logging.WARNING, logger="agentpit.api.app"):
        assert _start_admin_gas_loop(_Admin("ok"), settings) is None   # type: ignore[arg-type]
    assert len(_warnings(caplog)) == 1
    assert "NEVER refused" in _warnings(caplog)[0].getMessage()


def test_interval_zero_with_no_stop_level_is_a_deliberate_off(caplog):
    settings = Settings(AGENTPIT_ADMIN_GAS_CHECK_INTERVAL_SECONDS=0, AGENTPIT_ADMIN_GAS_STOP_GAS=0)  # type: ignore[call-arg]
    with caplog.at_level(logging.WARNING, logger="agentpit.api.app"):
        assert _start_admin_gas_loop(_Admin("ok"), settings) is None   # type: ignore[arg-type]
    assert not _warnings(caplog)


def test_a_positive_interval_starts_the_loop_quietly(caplog):
    async def start_and_stop():
        task = _start_admin_gas_loop(_Admin("ok"), Settings(AGENTPIT_ADMIN_GAS_CHECK_INTERVAL_SECONDS=3600))  # type: ignore[arg-type]
        assert task is not None
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    with caplog.at_level(logging.WARNING, logger="agentpit.api.app"):
        asyncio.run(start_and_stop())
    assert not _warnings(caplog)
