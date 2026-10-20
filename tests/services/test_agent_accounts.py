import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from eth_account.signers.local import LocalAccount

from agentpit.datastructures.user import User
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import AgentLimitError
from agentpit.services.agent_accounts import MAX_AGENTS_PER_OWNER, RECENT_AGENT_SECONDS, AgentAccounts
from tests.db_helpers import fresh_test_conn, fresh_test_db

OWNER = "user_owner"
CLAWBITS = "app.clawbits.ai"


class _Onboarder:
    def __init__(self, db: DbSession) -> None:
        self.db = db
        self.calls: list[str] = []

    def __call__(self, user_id: str, _acct: LocalAccount) -> User:
        self.calls.append(user_id)
        with self.db.write() as conn:
            TableWrite.mark_user_onboarded(conn, user_id)
            user = TableRead.get_user_by_userid(conn, user_id)
        assert user is not None
        return user


def _accounts() -> tuple[AgentAccounts, _Onboarder, DbSession]:
    db = fresh_test_db()
    onboard = _Onboarder(db)
    return AgentAccounts(db, onboard), onboard, db


def test_first_sight_creates_an_unfunded_agent_row():
    accounts, onboard, db = _accounts()

    agent = accounts.agent_for(OWNER, "Claude", None, None)

    assert agent.email is None
    assert agent.workos_user_id is None
    assert agent.handle
    assert agent.auto_redeem
    assert agent.onboarded_at is None
    assert onboard.calls == []
    with db.read() as conn:
        stored = TableRead.get_agent(conn, OWNER, "Claude", None)
    assert stored is not None and stored.user_id == agent.user_id


def test_one_agent_per_owner_and_app():
    accounts, _, _ = _accounts()

    claude = accounts.agent_for(OWNER, "Claude", None, None)

    assert accounts.agent_for(OWNER, "Claude", None, None).user_id == claude.user_id
    assert accounts.agent_for(OWNER, "Codex", None, None).user_id != claude.user_id
    assert accounts.agent_for("user_other", "Claude", None, None).user_id != claude.user_id


def test_each_client_of_one_app_gets_its_own_agent():
    accounts, _, _ = _accounts()

    first = accounts.agent_for(OWNER, "OpenClaw MCP", "client_1", CLAWBITS)
    second = accounts.agent_for(OWNER, "OpenClaw MCP", "client_2", CLAWBITS)

    assert first.user_id != second.user_id
    assert accounts.agent_for(OWNER, "OpenClaw MCP", "client_1", CLAWBITS).user_id == first.user_id


def test_the_first_clawbits_client_takes_over_the_shared_legacy_agent():
    accounts, _, _ = _accounts()
    legacy = accounts.agent_for(OWNER, "OpenClaw MCP", None, CLAWBITS)

    first = accounts.agent_for(OWNER, "OpenClaw MCP", "client_1", CLAWBITS)
    second = accounts.agent_for(OWNER, "OpenClaw MCP", "client_2", CLAWBITS)

    assert first.user_id == legacy.user_id
    assert second.user_id != legacy.user_id


def test_a_self_hosted_legacy_agent_is_not_taken_over():
    accounts, _, _ = _accounts()
    local = accounts.agent_for(OWNER, "OpenClaw MCP", None, "127.0.0.1")

    hosted = accounts.agent_for(OWNER, "OpenClaw MCP", "client_1", CLAWBITS)

    assert hosted.user_id != local.user_id
    assert accounts.agent_for(OWNER, "OpenClaw MCP", None, "127.0.0.1").user_id == local.user_id


def test_a_deleted_agent_s_app_starts_a_fresh_agent():
    accounts, _, db = _accounts()
    agent = accounts.agent_for(OWNER, "Claude", None, None)
    with db.write() as conn:
        TableWrite.delete_agent(conn, agent.user_id, 1)

    fresh = accounts.agent_for(OWNER, "Claude", None, None)

    assert fresh.user_id != agent.user_id


def test_an_api_agent_starts_unfunded_and_has_no_app():
    accounts, onboard, _ = _accounts()

    agent = accounts.create_api_agent(OWNER)

    assert agent.onboarded_at is None
    assert agent.agent_app is None and agent.handle
    assert onboard.calls == []


def test_the_latest_sign_in_host_is_kept():
    accounts, _, db = _accounts()
    agent = accounts.agent_for(OWNER, "Claude Code", None, "127.0.0.1")

    moved = accounts.agent_for(OWNER, "Claude Code", None, "localhost")

    assert moved.user_id == agent.user_id and moved.agent_host == "localhost"
    with db.read() as conn:
        stored = TableRead.get_agent(conn, OWNER, "Claude Code", None)
    assert stored is not None and stored.agent_host == "localhost"


def test_a_lost_create_race_returns_the_winner():
    accounts, _, _ = _accounts()
    winner = accounts.agent_for(OWNER, "Claude", None, None)

    assert accounts._create(OWNER, "Claude", None, None).user_id == winner.user_id


def test_ready_onboards_once():
    accounts, onboard, _ = _accounts()
    agent = accounts.agent_for(OWNER, "Claude", None, None)

    funded = accounts.ready(agent)

    assert funded.onboarded_at is not None
    assert accounts.ready(agent).onboarded_at is not None
    assert accounts.ready(funded) is funded
    assert onboard.calls == [agent.user_id]


def test_an_owner_at_the_limit_gets_no_new_agent_but_keeps_the_ones_it_has():
    accounts, onboard, db = _accounts()
    claude = accounts.agent_for(OWNER, "Claude", None, None)
    for _ in range(MAX_AGENTS_PER_OWNER - 1):
        accounts.create_api_agent(OWNER)

    with pytest.raises(AgentLimitError, match="You can have at most 10 agents"):
        accounts.create_api_agent(OWNER)
    with pytest.raises(AgentLimitError):
        accounts.agent_for(OWNER, "Codex", None, None)

    assert accounts.agent_for(OWNER, "Claude", None, None).user_id == claude.user_id
    assert accounts.create_api_agent("user_other").user_id
    with db.read() as conn:
        assert len(TableRead.agents_owned_by(conn, OWNER)) == MAX_AGENTS_PER_OWNER
    assert onboard.calls == []


def test_an_agent_deleted_within_a_day_of_its_creation_still_counts():
    accounts, _, db = _accounts()
    made = [accounts.create_api_agent(OWNER) for _ in range(MAX_AGENTS_PER_OWNER)]
    now = int(time.time())
    with db.write() as conn:
        TableWrite.delete_agent(conn, made[0].user_id, now)
        TableWrite.delete_agent(conn, made[1].user_id, now)
        conn.execute(
            "UPDATE users SET CREATED_AT = %s WHERE USER_ID = %s",
            (now - RECENT_AGENT_SECONDS - 1, made[1].user_id),
        )

    accounts.create_api_agent(OWNER)
    with pytest.raises(AgentLimitError):
        accounts.create_api_agent(OWNER)


def _attempt(accounts: AgentAccounts) -> str:
    try:
        accounts.create_api_agent(OWNER)
    except AgentLimitError:
        return "refused"
    return "created"


def _slow_count(monkeypatch) -> None:
    count = TableRead.count_owner_agents

    def slow_count(*args):
        made = count(*args)
        time.sleep(0.5)  # hold both creations between the count and the insert
        return made

    monkeypatch.setattr(TableRead, "count_owner_agents", staticmethod(slow_count))


def test_two_creations_racing_for_the_last_slot_make_one_agent(monkeypatch):
    accounts, _, _ = _accounts()
    for _ in range(MAX_AGENTS_PER_OWNER - 1):
        accounts.create_api_agent(OWNER)
    _slow_count(monkeypatch)

    with ThreadPoolExecutor(2) as pool:
        outcomes = sorted(pool.map(lambda _: _attempt(accounts), range(2)))

    assert outcomes == ["created", "refused"]


def test_two_first_sign_ins_from_one_app_at_the_last_slot_share_its_agent(monkeypatch):
    accounts, _, _ = _accounts()
    for _ in range(MAX_AGENTS_PER_OWNER - 1):
        accounts.create_api_agent(OWNER)
    _slow_count(monkeypatch)

    with ThreadPoolExecutor(2) as pool:
        first, again = pool.map(lambda _: accounts.agent_for(OWNER, "Claude", None, None), range(2))

    assert first.user_id == again.user_id


def test_an_owner_at_the_limit_is_refused_without_waiting_on_the_owner_lock():
    accounts, _, _ = _accounts()
    for _ in range(MAX_AGENTS_PER_OWNER):
        accounts.create_api_agent(OWNER)

    pool = ThreadPoolExecutor(1)
    try:
        with fresh_test_conn() as holder, holder.transaction():
            TableWrite.lock_owner(holder, OWNER)
            outcome = pool.submit(_attempt, accounts).result(timeout=5)
    finally:
        pool.shutdown()

    assert outcome == "refused"
