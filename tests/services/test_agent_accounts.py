from eth_account.signers.local import LocalAccount

from agentpit.datastructures.user import User
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.services.agent_accounts import AgentAccounts
from tests.db_helpers import fresh_test_db

OWNER = "user_owner"


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

    agent = accounts.agent_for(OWNER, "Claude")

    assert agent.email is None
    assert agent.workos_user_id is None
    assert agent.handle
    assert agent.auto_redeem
    assert agent.onboarded_at is None
    assert onboard.calls == []
    with db.read() as conn:
        stored = TableRead.get_agent(conn, OWNER, "Claude")
    assert stored is not None and stored.user_id == agent.user_id


def test_one_agent_per_owner_and_app():
    accounts, _, _ = _accounts()

    claude = accounts.agent_for(OWNER, "Claude")

    assert accounts.agent_for(OWNER, "Claude").user_id == claude.user_id
    assert accounts.agent_for(OWNER, "Codex").user_id != claude.user_id
    assert accounts.agent_for("user_other", "Claude").user_id != claude.user_id


def test_a_lost_create_race_returns_the_winner():
    accounts, _, _ = _accounts()
    winner = accounts.agent_for(OWNER, "Claude")

    assert accounts._create(OWNER, "Claude").user_id == winner.user_id


def test_ready_onboards_once():
    accounts, onboard, _ = _accounts()
    agent = accounts.agent_for(OWNER, "Claude")

    funded = accounts.ready(agent)

    assert funded.onboarded_at is not None
    assert accounts.ready(agent).onboarded_at is not None
    assert accounts.ready(funded) is funded
    assert onboard.calls == [agent.user_id]
