class DomainError(Exception):
    """Base class for application-domain errors translated to HTTP responses."""


class NotFoundError(DomainError):
    pass


class AlreadyExistsError(DomainError):
    pass


class BusinessRuleError(DomainError):
    pass


class MarketNotFoundError(NotFoundError):
    def __init__(self, market_id: int):
        super().__init__("Market not found")
        self.market_id = market_id


class EventNotFoundError(NotFoundError):
    def __init__(self, slug: str):
        super().__init__("Event not found")
        self.slug = slug


class PersonalityNotFoundError(NotFoundError):
    def __init__(self, personality_id: str):
        super().__init__(f"Personality '{personality_id}' not found")
        self.personality_id = personality_id


class UserAlreadyExistsError(AlreadyExistsError):
    def __init__(self, identifier: str):
        super().__init__(f"User '{identifier}' already exists")
        self.identifier = identifier


class HandleAlreadyExistsError(AlreadyExistsError):
    def __init__(self, handle: str):
        super().__init__(f"Handle '{handle}' is already in use")
        self.handle = handle


class UserNotFoundError(NotFoundError):
    def __init__(self, message: str = "User not found"):
        super().__init__(message)


class InvalidCredentialsError(BusinessRuleError):
    pass


class AuthCodeRateLimitedError(DomainError):
    """Too many code requests for this address or from this caller.

    Not a `BusinessRuleError`: that maps to 400, and this is a 429 carrying a
    `Retry-After`. It is deliberately indistinguishable in wording from
    WorkOS's own rate limit -- a caller learns that they must wait, and nothing
    about whether the ceiling they hit was ours or the provider's.
    """

    def __init__(self, retry_after: int):
        super().__init__("too many attempts — wait a moment and try again")
        self.retry_after = retry_after


class FeatureDisabledError(DomainError):
    """Raised when a feature is switched off by configuration rather than broken."""


class OnboardingError(BusinessRuleError):
    """Raised when on-chain onboarding fails after the DB row is created."""


class AgentAlreadyExistsError(AlreadyExistsError):
    def __init__(self, agent_id: str):
        super().__init__(f"Agent '{agent_id}' already exists")
        self.agent_id = agent_id


class InsufficientBalanceError(BusinessRuleError):
    pass


class OrderNotFilledError(BusinessRuleError):
    pass


class InvalidPaginationError(BusinessRuleError):
    pass


class MarketStateError(BusinessRuleError):
    """Raised when an operation is invalid for the market's current state."""


class InsufficientGasError(BusinessRuleError):
    """Raised when a user's wallet can't cover a transaction's gas.

    The platform pays: `UserGasSponsor` tops the wallet up to exactly what the
    transaction needs before sending it. So this means the node still refused
    for balance after the sponsor's one resize-and-retry, or the operator
    switched sponsorship off (`AGENTPIT_SPONSOR_USER_GAS=false`). 402, distinct
    from the generic `BusinessRuleError` 400, so a caller can tell "your input
    is wrong" from "the wallet could not pay". Neither is anything the user can
    fund, so the UI says only that the action is unavailable right now.
    """


class AdminGasPausedError(DomainError):
    """The admin wallet, which pays for users' fills and tops up the gas of
    every transaction they sign, is below its stop level, so sponsored sends
    are refused until it is refilled.

    A direct `DomainError` (503), not a `BusinessRuleError` (400): nothing the
    caller did is wrong, and it must not read as "wallet still being set up"
    the way an `OnboardingError` does over MCP. The default message names no
    action: a claim, a split or a signup meets it as often as an order does.
    """

    def __init__(
        self,
        message: str = "the platform's gas wallet is running low — try again later",
    ):
        super().__init__(message)


class GasTopUpTimeoutError(DomainError):
    """The admin's gas top-up for a user-signed transaction got no receipt in
    time, found no free admin transaction slot, or was lost by the node
    (`TxDropped`: its nonce went to a gap filler or another writer), so no
    transaction of the user's was sent.

    A direct `DomainError` (503) like `AdminGasPausedError`: our side is
    congested, nothing the caller did is wrong, and the same request succeeds
    once it clears. The top-up may still mine (a lost one will not), so a retry
    sizes against the balance it finds and tops up only the difference.
    """

    def __init__(
        self, message: str = "the platform is busy — try again in a moment"
    ):
        super().__init__(message)


class GasPriceMovedError(DomainError):
    """The network fee rose while a user-signed transaction was being sent.
    The node refused it at import as underpriced, `UserGasSponsor` re-sized it
    at the new price and topped the wallet up again, and the node refused the
    retry as underpriced too. Neither can mine, so nothing of the user's is in
    flight.

    A direct `DomainError` (503) like `GasTopUpTimeoutError`: nothing the
    caller did is wrong, and the same request goes through once the fee
    settles.
    """

    def __init__(
        self,
        message: str = "the network fee rose while sending — try again in a moment",
    ):
        super().__init__(message)


class GasBudgetExceededError(DomainError):
    """The account has made the admin pay for its daily share of gas: fills,
    splits and merges. Claims and onboarding are booked against the same
    daily row but never refused for it.

    429 with `Retry-After` (seconds to the next UTC midnight), like
    `AuthCodeRateLimitedError`. The message is self-contained because MCP
    callers see only the text, not the header.
    """

    def __init__(self, retry_after: int):
        super().__init__(
            "this account has used its daily gas budget — it resets at 00:00 UTC"
        )
        self.retry_after = retry_after


class NothingToClaimError(BusinessRuleError):
    """A claim whose on-chain payout is below the minimum
    (`AGENTPIT_MIN_CLAIM_MICRO`): no holdings, only losing tokens, or dust.

    Raised before anything is sent. `redeemPositions` succeeds even with zero
    holdings, so without this gate every such claim would be a sponsored
    transaction that pays out nothing.
    """

    def __init__(self, message: str = "nothing to claim"):
        super().__init__(message)


class TransactionRevertedError(BusinessRuleError):
    """A transaction the platform paid the gas for was mined with status 0.

    Raised only after its gas has been booked: a reverted transaction is
    still paid for. The caller writes no REDEEM/SPLIT/MERGE row for it.
    """

    def __init__(self, message: str):
        super().__init__(message)


class TransactionPendingError(DomainError):
    """A split, merge or claim was signed and sent, and nobody knows yet how
    it ended: its receipt did not come back in time, the node never answered
    the broadcast, the receipt poll failed after the node took it, or it failed
    with an error not recognised as a refusal. It may well mine.

    Its intent row stays in `pending_user_txs`, so the reconciler writes the
    history row once it has mined (`reconcile_pending_user_txs`, run by both
    resolution loops), and until then a second split, merge or claim on that
    market is refused (409) instead of repeating it. A direct `DomainError`
    (503): nothing the caller did is wrong, but they must not simply send it
    again.
    """

    def __init__(
        self,
        message: str = (
            "the transaction was sent but is not confirmed yet — it will appear "
            "in your history once it lands; do not repeat it"
        ),
    ):
        super().__init__(message)


class TransactionInProgressError(DomainError):
    """Another transaction for this account is still being sent.

    Claim, split, merge and onboarding share one per-account lock because
    they share the account's nonce stream; the lock is taken without waiting.
    409, not a `BusinessRuleError` (400): nothing in the request is wrong,
    and the same request succeeds once the other one has landed.
    """

    def __init__(
        self,
        message: str = "another transaction for this account is in progress — try again in a moment",
    ):
        super().__init__(message)
