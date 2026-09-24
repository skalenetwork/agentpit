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

    The house no longer tops accounts up automatically -- the wallet is
    theirs to fund. Distinct from the generic `BusinessRuleError` 400 so the
    UI can tell "your input is wrong" from "your wallet needs funding" and
    show the right recourse.
    """
