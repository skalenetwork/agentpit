import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class Deployment(BaseModel):
    """Frozen view of deployments/local.json — addresses for a single chain."""

    model_config = ConfigDict(frozen=True)

    chain_id: int
    rpc_url: str
    admin: str
    usd: str
    faucet: str
    ctf: str
    proxy_factory: str
    safe_factory: str
    exchange: str
    signup_grant_raw: int

    @classmethod
    def load(cls, path: Path) -> "Deployment":
        raw = json.loads(Path(path).read_text())
        # signup_grant_raw is written as a string from bash; coerce to int
        if isinstance(raw.get("signup_grant_raw"), str):
            raw["signup_grant_raw"] = int(raw["signup_grant_raw"])
        return cls.model_validate(raw)


#: anvil's default chain id: the only chain the stack runs on that can be
#: wiped out from under a surviving database.
ANVIL_CHAIN_ID = 31337


def is_disposable_chain(chain_id: int) -> bool:
    """True only for a local anvil. `simulated_chain` (re-grant gas on a
    login with a zero balance) is a repair for a wiped chain and a faucet on
    any other, so it is honoured only here whatever the setting says."""
    return chain_id == ANVIL_CHAIN_ID
