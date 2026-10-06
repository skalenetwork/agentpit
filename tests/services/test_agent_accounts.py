import pytest
from eth_account.signers.local import LocalAccount

from agentpit.datastructures.user import User
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import OnboardingError
from agentpit.services.agent_accounts import AgentAccounts
from tests.db_helpers import fresh_test_db

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


def test_an_api_agent_is_funded_at_once_and_has_no_app():
    accounts, onboard, _ = _accounts()

    agent = accounts.create_api_agent(OWNER)

    assert agent.onboarded_at is not None
    assert agent.agent_app is None and agent.handle
    assert onboard.calls == [agent.user_id]


def test_an_api_agent_that_cannot_be_funded_is_not_left_behind():
    db = fresh_test_db()

    def fail(_user_id: str, _acct: LocalAccount) -> User:
        raise OnboardingError("chain down")

    with pytest.raises(OnboardingError):
        AgentAccounts(db, fail).create_api_agent(OWNER)

    with db.read() as conn:
        assert TableRead.agents_owned_by(conn, OWNER) == []


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
