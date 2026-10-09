import logging
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        populate_by_name=True,
    )

    database_url: str = Field(
        default="postgresql:///agentpit",
        validation_alias="AGENTPIT_DATABASE_URL",
    )
    # Pool floor. 2 keeps warm connections in prod; tests set 0 so the many
    # short-lived create_app() pools don't each pin idle connections.
    pool_min_size: int = Field(default=2, validation_alias="AGENTPIT_POOL_MIN_SIZE")
    pool_max_idle: float = Field(
        default=600.0, validation_alias="AGENTPIT_POOL_MAX_IDLE"
    )
    sync_enabled: bool = Field(default=False, validation_alias="SYNC")
    sync_min_volume_24h: float = Field(
        default=1_000.0, validation_alias="AGENTPIT_SYNC_MIN_VOLUME_24H"
    )
    sync_game_tag_ids: list[int] = Field(
        default=[100351, 450], validation_alias="AGENTPIT_SYNC_GAME_TAG_IDS"
    )
    sync_min_game_liquidity: float = Field(
        default=10_000.0, validation_alias="AGENTPIT_SYNC_MIN_GAME_LIQUIDITY"
    )
    sync_series_ids: list[int] = Field(
        default=[], validation_alias="AGENTPIT_SYNC_SERIES_IDS"
    )
    # Drop the two upstream series that regenerate faster than anyone reads
    # them: the daily temperature markets (49 cities x ~3.4 thresholds = ~166
    # born every day, median life 55.9h -- 11% of the standing catalogue but 23%
    # of every market ever created and resolved) and the sports prop tail
    # (spreads, totals, team totals, per-half, nrfi -- all hung off a
    # game we already carry). Together they are 89% of new market creations, and
    # each creation costs prepareCondition + registerToken + a first
    # splitPosition on chain with a reportPayouts at the end: ~870M gas/day,
    # real money once the chain moves to SKALE on Base. True is the decision
    # already made; the flag exists so it can be reversed without a code change.
    sync_exclude_churn_series: bool = Field(
        default=True, validation_alias="AGENTPIT_SYNC_EXCLUDE_CHURN_SERIES"
    )
    # Categories the product does not carry at all. Unlike the churn filter
    # above -- which drops the prop tail and keeps the headline game -- this
    # drops the whole category: no sync, no listing, no mirrored liquidity.
    #
    # Matched case-insensitively against the labels in CATEGORY_PRIORITY.
    excluded_categories: list[str] = Field(
        default=[], validation_alias="AGENTPIT_EXCLUDED_CATEGORIES"
    )
    # Matched against `market_tags.SLUG`, which is Polymarket's own slug — an
    # event is excluded when ANY of its markets carries one of these.
    excluded_tags: list[str] = Field(
        default=[], validation_alias="AGENTPIT_EXCLUDED_TAGS"
    )

    order_cleanup_interval_seconds: float = Field(
        default=60.0, validation_alias="AGENTPIT_ORDER_CLEANUP_INTERVAL_SECONDS"
    )
    idempotency_key_retention_seconds: int = Field(
        default=86400, validation_alias="AGENTPIT_IDEMPOTENCY_KEY_RETENTION_SECONDS"
    )

    leaderboard_enabled: bool = Field(
        default=False, validation_alias="AGENTPIT_LEADERBOARD_ENABLED"
    )
    # Google sign-in's audience check. Empty means the feature is off: no
    # verifier is built and POST /auth/google answers 503. A client id is public
    # by design — it appears in the page of every site that uses Google sign-in
    # — and this flow has no client secret at all.
    google_client_id: str = Field(default="", validation_alias="GOOGLE_CLIENT_ID")

    @field_validator("google_client_id", mode="after")
    @classmethod
    def _strip_google_client_id(cls, value: str) -> str:
        # Docker Compose's env_file parser does not reliably strip trailing
        # whitespace. A value that is blank-but-present would otherwise build a
        # verifier whose audience matches nothing -- 401ing every sign-in while
        # looking configured -- instead of the intended "off".
        return value.strip()

    # WorkOS AuthKit. An absent api key means the feature is simply not
    # present, the same shape as GOOGLE_CLIENT_ID above -- nothing raises at
    # startup and every AuthKit path answers as unconfigured.
    #
    # `workos_client_id` is load-bearing beyond identifying the application:
    # both the token issuer and the JWKS URL are DERIVED from it (see
    # auth/authkit_tokens.py), so it is the one value that must be right.
    #
    # `workos_authkit_domain` is the hosted sign-in surface and the issuer of
    # the OAuth tokens MCP clients send to /mcp. It is NOT the issuer of the
    # SPA's session tokens -- those say api.workos.com/user_management/
    # <client_id>, verified against staging on 2026-08-11. /mcp is served only
    # when it is an https URL.
    workos_api_key: str = Field(default="", validation_alias="WORKOS_API_KEY")
    workos_client_id: str = Field(default="", validation_alias="WORKOS_CLIENT_ID")
    workos_authkit_domain: str = Field(
        default="", validation_alias="WORKOS_AUTHKIT_DOMAIN"
    )

    @field_validator(
        "workos_api_key", "workos_client_id", "workos_authkit_domain", mode="after"
    )
    @classmethod
    def _strip_workos(cls, value: str) -> str:
        # Same measured reason as `_strip_google_client_id` above: Compose's
        # env_file parser leaves trailing whitespace. Here it is worse than a
        # bad audience -- a padded domain builds a JWKS URL containing "%20",
        # so the fetch 404s and EVERY sign-in is rejected as "invalid session"
        # with no configuration error anywhere to point at the cause.
        return value.strip()

    @field_validator("workos_authkit_domain", mode="after")
    @classmethod
    def _normalize_authkit_domain(cls, value: str) -> str:
        """Trailing slash off, and a missing scheme complained about loudly.

        Both shapes are what an operator actually pastes. A trailing slash was
        silently tolerated in one place and not the other -- the JWKS fetch
        stripped it, so the key resolved and the config looked right, while the
        `iss` comparison kept the slash and rejected every token.

        This used to raise on a missing scheme. It must not: `Settings()` is
        constructed by `create_app` before anything serves, so a ValueError
        here crash-loops the whole api container -- taking `/order` down for
        every trading bot. SPA sign-in does not read this value; the MCP
        endpoint does, and `create_app` leaves /mcp off unless it is https.
        """
        value = value.rstrip("/")
        if value and not value.startswith(("http://", "https://")):
            log.error(
                "WORKOS_AUTHKIT_DOMAIN=%r has no scheme; it should look like "
                "https://%s. SPA sign-in is unaffected, but /mcp stays off.",
                value,
                value,
            )
        return value

    mcp_url: str = Field(
        default="https://api.agentpit.dev/mcp", validation_alias="AGENTPIT_MCP_URL"
    )
    landing_url: str = Field(
        default="https://agentpit.dev", validation_alias="AGENTPIT_LANDING_URL"
    )

    leaderboard_interval_seconds: int = Field(
        default=300, validation_alias="AGENTPIT_LEADERBOARD_INTERVAL_SECONDS"
    )
    cors_origins: list[str] = Field(
        default=["http://localhost:5173"], validation_alias="AGENTPIT_CORS_ORIGINS"
    )

    # Auth
    # `POST /auth/code` is unauthenticated and, since the cutover, the only
    # door into the product -- and every request past the limit costs a WorkOS
    # email. Both are per hour, in a fixed window.
    #
    # Per address is the honest-user number: somebody whose mail is slow closes
    # the dialog and tries again, and the UI does NOT stop them (its cooldown
    # guards only the resend button, not a fresh submit), so this has to be
    # generous enough to forgive that.
    #
    # Per IP is deliberately higher than per address rather than equal: an
    # office behind one NAT is several people, while one address is one person.
    # It exists to catch address ROTATION, which the per-address rule cannot
    # see at all.
    auth_code_per_email_hourly: int = Field(
        default=20, validation_alias="AGENTPIT_AUTH_CODE_PER_EMAIL_HOURLY"
    )
    auth_code_per_ip_hourly: int = Field(
        default=60, validation_alias="AGENTPIT_AUTH_CODE_PER_IP_HOURLY"
    )

    jwt_secret: str = Field(
        default="dev-only-insecure-secret-change-me",
        validation_alias="JWT_SECRET",
    )
    jwt_algorithm: str = Field(default="HS256", validation_alias="JWT_ALGORITHM")
    jwt_expires_seconds: int = Field(
        default=60 * 60 * 24, validation_alias="JWT_EXPIRES_SECONDS"
    )

    # On-chain stack
    deployment_path: Path = Field(
        default=Path("deployments/local.json"),
        validation_alias="AGENTPIT_DEPLOYMENT_PATH",
    )
    operator_private_key: str | None = Field(default=None, validation_alias="PK")
    rpc_url_override: str | None = Field(default=None, validation_alias="RPC_URL")
    # Gas for the three transactions a new account must send before it can
    # trade: approve(exchange), approve(ctf), setApprovalForAll(exchange).
    # Measured at 138,946 gas across all 16 accounts on the production chain.
    # At SKALE Base's 47.6 gwei that is 0.0066 native; this is 3x that, which
    # also covers a few later claims at 91,743 gas each. The previous default
    # was 10**18 — 21,000,000 gas, 150x the need — which cost $0.25 a signup
    # on a chain where the native coin is bought with USDC.
    signup_gas_grant_wei: int = Field(
        default=2 * 10**16, validation_alias="AGENTPIT_SIGNUP_GAS_GRANT_WEI"
    )
    # True while the chain can be wiped out from under the database (a local
    # anvil): a zero native balance then means "the chain forgot this account"
    # and re-running onboarding is the repair. On a durable chain a zero balance
    # means the opposite -- the account simply spent its gas -- and re-granting
    # would turn login into a treasury faucet, repeatable by anyone willing to
    # empty their own wallet. Set false before pointing at a real chain: the
    # signup grant then becomes once per account rather than once per drain.
    simulated_chain: bool = Field(
        default=True, validation_alias="AGENTPIT_SIMULATED_CHAIN"
    )
    tx_confirmations_timeout_s: int = Field(
        default=30, validation_alias="AGENTPIT_TX_TIMEOUT_S"
    )
    # How many admin-key transactions may wait for a block at once. Our SKALE
    # chain runs in Multi-Transaction Mode, so they share blocks instead of
    # landing one per block; the sender drops to 1 by itself if the node turns
    # out to refuse a second one. 1 = strictly serial. At most 256: skaled's
    # queue holds ~1024 transactions, shared by every sender on the chain.
    # The sync sends a quarter of it per JSON-RPC batch: 128 = 32 markets, up
    # to 64 transactions in one batch.
    admin_tx_max_in_flight: int = Field(
        default=128, ge=1, le=256, validation_alias="AGENTPIT_ADMIN_TX_MAX_IN_FLIGHT"
    )

    # Admin
    admin_token: str = Field(
        default="dev-admin-token",
        validation_alias="AGENTPIT_ADMIN_TOKEN",
    )

    # Liquidity Engine (Phase 5c: Polymarket book mirror)
    liquidity_engine_enabled: bool = Field(
        default=False, validation_alias="LIQUIDITY_ENGINE"
    )
    # apUSD is 6-decimal, so every figure here is raw. The house pays its side
    # of every fill from this; 1e18 apUSD is headroom chosen deliberately.
    house_mint_raw: int = Field(
        default=10**24, validation_alias="AGENTPIT_HOUSE_MINT_RAW"
    )
    # What a user's paper balance is restored to. $100,000.
    paper_balance_target_raw: int = Field(
        default=100_000_000_000, validation_alias="AGENTPIT_PAPER_BALANCE_TARGET_RAW"
    )
    topup_cooldown_seconds: int = Field(
        default=86_400, validation_alias="AGENTPIT_TOPUP_COOLDOWN_SECONDS"
    )
    mirror_assets_per_connection: int = Field(
        default=120, validation_alias="AGENTPIT_MIRROR_ASSETS_PER_CONNECTION"
    )
    mirror_watchdog_seconds: float = Field(
        default=5.0, validation_alias="AGENTPIT_MIRROR_WATCHDOG_SECONDS"
    )
    mirror_tape_enabled: bool = Field(
        default=True, validation_alias="AGENTPIT_MIRROR_TAPE_ENABLED"
    )
    # How often the mirror re-scans the active-market set. Kept short so a new
    # market is picked up and quoted promptly; a no-change scan is a cheap DB
    # read.
    mirror_target_refresh_seconds: float = Field(
        default=15.0, validation_alias="AGENTPIT_MIRROR_TARGET_REFRESH_SECONDS"
    )
