import re
from dataclasses import dataclass
from agentpit.common import check_state


@dataclass(frozen=True)
class ConditionId:
    value: str

    def __post_init__(self):
        check_state(len(self.value) > 0, "Condition ID value must not be empty")
