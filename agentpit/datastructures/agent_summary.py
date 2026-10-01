from pydantic import BaseModel

from agentpit.datastructures.user import User
from agentpit.domain.runner import Runner, runner_for


class AgentSummary(BaseModel):
    handle: str | None
    eth_address: str
    created_at: int
    runner: Runner

    @classmethod
    def of(cls, user: User) -> "AgentSummary":
        return cls(
            handle=user.handle,
            eth_address=user.eth_address,
            created_at=user.created_at,
            runner=runner_for(user.agent_app, user.agent_host),
        )


class NewAgent(AgentSummary):
    api_key: str
