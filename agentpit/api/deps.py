from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from agentpit.auth.google import GoogleTokenVerifier
from agentpit.auth.jwt import JwtCoder
from agentpit.auth.workos_client import WorkOsClient
from agentpit.config import Settings
from agentpit.datastructures.user import User
from agentpit.db.session import DbSession
from agentpit.domain.exceptions import FeatureDisabledError
from agentpit.onchain.admin import OnchainAdmin
from agentpit.services.account_service import AccountService
from agentpit.services.agent_accounts import AgentAccounts
from agentpit.services.agent_service import AgentService
from agentpit.services.auth_service import AuthService
from agentpit.services.authkit_service import AuthKitService
from agentpit.services.balance_service import BalanceService
from agentpit.services.event_service import EventService
from agentpit.services.leaderboard_service import LeaderboardService
from agentpit.services.market_service import MarketService
from agentpit.services.order_service import OrderService
from agentpit.services.personality_service import PersonalityService
from agentpit.services.position_service import PositionService
from agentpit.services.trade_service import TradeService
from agentpit.services.usdc_service import UsdcService


# --- placeholders overridden by the app factory ---------------------------


def get_db_session() -> DbSession:
    raise RuntimeError("get_db_session has not been overridden by the app factory")


def get_settings() -> Settings:
    raise RuntimeError("get_settings has not been overridden by the app factory")


def get_jwt_coder() -> JwtCoder:
    raise RuntimeError("get_jwt_coder has not been overridden by the app factory")


def get_onchain_admin() -> OnchainAdmin:
    raise RuntimeError("get_onchain_admin has not been overridden by the app factory")


def get_google_verifier() -> GoogleTokenVerifier | None:
    raise RuntimeError("get_google_verifier has not been overridden by the app factory")


def get_workos_client() -> WorkOsClient | None:
    raise RuntimeError("get_workos_client has not been overridden by the app factory")


def get_current_user() -> User:
    raise RuntimeError("get_current_user has not been overridden by the app factory")


# --- common annotated types ----------------------------------------------

SessionDep = Annotated[DbSession, Depends(get_db_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
JwtCoderDep = Annotated[JwtCoder, Depends(get_jwt_coder)]
OnchainAdminDep = Annotated[OnchainAdmin, Depends(get_onchain_admin)]
GoogleVerifierDep = Annotated[
    GoogleTokenVerifier | None, Depends(get_google_verifier)
]
WorkOsClientDep = Annotated[WorkOsClient | None, Depends(get_workos_client)]
CurrentUserDep = Annotated[User, Depends(get_current_user)]


# --- auth guards -----------------------------------------------------------


def require_admin_token(
    settings: SettingsDep,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
) -> None:
    """Operator gate: X-Admin-Token header must match Settings.admin_token."""
    if x_admin_token is None or x_admin_token != settings.admin_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="admin token missing or invalid",
        )


# --- service factories ---------------------------------------------------


def get_market_service(
    db: SessionDep, onchain: OnchainAdminDep, settings: SettingsDep
) -> MarketService:
    return MarketService(db, onchain, settings.excluded_categories, settings.excluded_tags)


def get_event_service(db: SessionDep, settings: SettingsDep) -> EventService:
    return EventService(db, settings.excluded_categories, settings.excluded_tags)


def get_usdc_service(db: SessionDep, onchain: OnchainAdminDep) -> UsdcService:
    return UsdcService(db, onchain)


def get_position_service(db: SessionDep, onchain: OnchainAdminDep) -> PositionService:
    return PositionService(db, onchain)


def get_personality_service(db: SessionDep) -> PersonalityService:
    return PersonalityService(db)


def get_agent_service(db: SessionDep) -> AgentService:
    return AgentService(db)


def get_auth_service(
    db: SessionDep,
    coder: JwtCoderDep,
    onchain: OnchainAdminDep,
    settings: SettingsDep,
    google: GoogleVerifierDep,
    workos: WorkOsClientDep,
) -> AuthService:
    return AuthService(db, coder, onchain, settings, google, workos)


def get_order_service(
    db: SessionDep, onchain: OnchainAdminDep, settings: SettingsDep
) -> OrderService:
    return OrderService(db, onchain, settings)


def get_trade_service(db: SessionDep) -> TradeService:
    return TradeService(db)


def get_account_service(db: SessionDep, onchain: OnchainAdminDep) -> AccountService:
    return AccountService(db, onchain)


AccountServiceDep = Annotated[AccountService, Depends(get_account_service)]


def get_balance_service(
    db: SessionDep,
    onchain: OnchainAdminDep,
    settings: SettingsDep,
    accounts: AccountServiceDep,
) -> BalanceService:
    return BalanceService(db, onchain, settings, accounts)


def get_leaderboard_service(
    db: SessionDep,
    onchain: OnchainAdminDep,
    accounts: AccountServiceDep,
    settings: SettingsDep,
) -> LeaderboardService:
    return LeaderboardService(db, onchain, accounts, settings)


LeaderboardServiceDep = Annotated[
    LeaderboardService, Depends(get_leaderboard_service)
]


BalanceServiceDep = Annotated[BalanceService, Depends(get_balance_service)]
MarketServiceDep = Annotated[MarketService, Depends(get_market_service)]
EventServiceDep = Annotated[EventService, Depends(get_event_service)]
UsdcServiceDep = Annotated[UsdcService, Depends(get_usdc_service)]
PositionServiceDep = Annotated[PositionService, Depends(get_position_service)]
PersonalityServiceDep = Annotated[PersonalityService, Depends(get_personality_service)]
AgentServiceDep = Annotated[AgentService, Depends(get_agent_service)]
AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
OrderServiceDep = Annotated[OrderService, Depends(get_order_service)]
TradeServiceDep = Annotated[TradeService, Depends(get_trade_service)]


# Below `AuthServiceDep` because it depends on it, and these annotations are
# evaluated at def time.
def get_authkit_service(
    db: SessionDep,
    workos: WorkOsClientDep,
    auth: AuthServiceDep,
    settings: SettingsDep,
) -> AuthKitService:
    if workos is None:
        # No WORKOS_API_KEY: the same shape Google sign-in takes without a
        # client id (auth_service.py:85). Raised here rather than in each of
        # the three routes so none of them can forget it.
        raise FeatureDisabledError("email code sign-in is not configured")
    # `_onboard_new_account` is private to `AuthService` and reached anyway:
    # `AuthKitService` takes it as an argument precisely so the two services do
    # not import each other, and a second copy of that work here is the drift
    # `_onboard_new_account`'s own docstring exists to prevent. `_maybe_reonboard`
    # is reached the same way and for the same reason.
    return AuthKitService(
        db=db,
        workos=workos,
        onboard=auth._onboard_new_account,
        reonboard=auth._maybe_reonboard,
        per_email_hourly=settings.auth_code_per_email_hourly,
        per_ip_hourly=settings.auth_code_per_ip_hourly,
    )


AuthKitServiceDep = Annotated[AuthKitService, Depends(get_authkit_service)]


def get_agent_accounts(db: SessionDep, auth: AuthServiceDep) -> AgentAccounts:
    return AgentAccounts(db, auth._onboard_new_account)


AgentAccountsDep = Annotated[AgentAccounts, Depends(get_agent_accounts)]


def get_owner(
    user: CurrentUserDep,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> str:
    if x_api_key is not None or user.workos_user_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="sign in to the AgentPit dashboard to manage agents",
        )
    return user.workos_user_id


OwnerDep = Annotated[str, Depends(get_owner)]
