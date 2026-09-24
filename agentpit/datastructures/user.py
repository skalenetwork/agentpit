import re
from eth_account.signers.local import LocalAccount
from pydantic import BaseModel, ConfigDict
from agentpit.common import check_state


class User(BaseModel):
    """Internal user record. Email + password are auth credentials; eth_key is server-held."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    user_id: str
    email: str | None
    eth_key: LocalAccount
    eth_address: str
    api_key: str
    handle: str | None = None
    onboarded_at: int | None = None
    created_at: int
    is_bot: bool = False
    # No default: `_row_to_user` is the only construction site and always
    # supplies this. A defaulted value fails OPEN -- it would show the
    # Google-only export control to a password account if a caller ever
    # forgot to pass it.
    has_password: bool
    # No default either: a defaulted value fails OPEN here too -- it would
    # start spending an account's gas on its behalf if a caller ever forgot
    # to pass it. AUTO_REDEEM_ENABLED defaults FALSE at the column, so every
    # existing account opts out until it says otherwise.
    auto_redeem: bool
    # The WorkOS AuthKit identity for this account, once it has one. Defaulted,
    # unlike the two above, because a missing value fails CLOSED here -- an
    # account with no WorkOS id simply cannot be found by WorkOS id, which is
    # exactly what an unmigrated row should do.
    workos_user_id: str | None = None
    agent_app: str | None = None

    def model_post_init(self, __context):
        if self.handle is not None:
            check_state(
                bool(re.match(r"^[a-zA-Z0-9_]{1,15}$", self.handle)),
                "handle must be a valid handle (1-15 characters, alphanumeric or underscores only)",
            )
