"""The positions view and the claim must agree on the claim minimum.

`PositionService.redeem` refuses a payout below AGENTPIT_MIN_CLAIM_MICRO. If
the account service were built with a different figure, a row could offer a
Claim button that can only answer 400, or hide one that would succeed.
"""

from agentpit.api.deps import get_account_service
from agentpit.config import Settings


def test_the_request_scoped_account_service_uses_the_configured_minimum():
    settings = Settings(_env_file=None, min_claim_micro=123_456)

    accounts = get_account_service(None, None, settings)  # type: ignore[arg-type]

    assert accounts._min_claim_micro == 123_456  # noqa: SLF001
