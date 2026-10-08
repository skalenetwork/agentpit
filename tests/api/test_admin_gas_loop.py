import asyncio
import logging

from agentpit.api.app import _admin_gas_loop
from agentpit.config import Settings


class _Admin:
    def __init__(self, state):
        self.state = state

    def refresh_admin_gas(self):
        return 3 * 10**18, self.state


async def _one_pass(admin):
    task = asyncio.create_task(_admin_gas_loop(admin, Settings(AGENTPIT_ADMIN_GAS_CHECK_INTERVAL_SECONDS=3600)))
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
