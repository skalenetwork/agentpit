import logging
from pathlib import Path

from pydantic import Field, field_validator, model_validator
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
    sync_interval_seconds: int = Field(
        default=60 * 60, validation_alias="AGENTPIT_SYNC_INTERVAL_SECONDS"
    )
    # Trending sync (top-N by 24h volume) + decoupled resolution/redeem loop
    sync_max_markets: int = Field(
        default=300, validation_alias="SYNC_MAX_MARKETS"
    )
    sync_liquidity_min: float = Field(
        default=0.0, validation_alias="SYNC_LIQUIDITY_MIN"
    )
    # When a market qualifies, its sibling outcomes come with it, capped at
    # this many per event (busiest first by 24h volume). The median upstream
    # event has 11 open outcomes, so 12 lets most through whole; the largest
    # carry 128 and nobody trades their tail. Measured cost at 12: 2302
    # markets per pass against 1000 without it. 0 disables the expansion.
    sync_event_max_outcomes: int = Field(
        default=12, validation_alias="SYNC_EVENT_MAX_OUTCOMES"
    )
    # Drop the two upstream series that regenerate faster than anyone reads
    # them: the daily temperature markets (49 cities x ~3.4 thresholds = ~166
    # born every day, median life 55.9h -- 11% of the standing catalogue but 23%
    # of every market ever created and resolved) and the sports prop tail
    # (spreads, totals, team totals, per-half, per-map, nrfi -- all hung off a
    # game we already carry). Together they are 89% of new market creations, and
    # each creation costs prepareCondition + registerToken + a first
    # splitPosition on chain with a reportPayouts at the end: ~870M gas/day,
    # real money once the chain moves to SKALE on Base. Decided by upstream
    # fields only (feeType / sportsMarketType), never by parsing slugs --
    # see `_is_churn_series`. True is the decision already made; the flag exists
    # so it can be reversed without a code change.
    sync_exclude_churn_series: bool = Field(
        default=True, validation_alias="AGENTPIT_SYNC_EXCLUDE_CHURN_SERIES"
    )
    # Categories the product does not carry at all. Unlike the churn filter
    # above -- which drops the prop tail and keeps the headline game -- this
    # drops the whole category: no sync, no listing, no mirrored liquidity.
    #
    # Sports is here because the UI has no rendering for it: a match resolves
    # in hours and its book empties the moment it does, so the grid fills with
    # rows that read as broken (see the "<1% chance" beside a 71% chart that
    # started this). Esports needs no entry of its own -- `resolve_category`
    # files it under Sports, which is 68.6% of the standing catalogue (1097 of
    # 1600 events measured on production 2026-08-12), so this is the single
    # biggest lever on gas and anvil growth as well.
    #
    # Matched case-insensitively against the labels in CATEGORY_PRIORITY.
    # Empty list = carry everything, which is the pre-2026-08-12 behaviour.
    excluded_categories: list[str] = Field(
        default=["Sports"], validation_alias="AGENTPIT_EXCLUDED_CATEGORIES"
    )
    # The same decision, reached through the tag graph instead of the CATEGORY
    # column. Upstream does not file everything sporting under Sports: three
    # esports events (two season-winner futures and a game-release question)
    # sat in the catalogue carrying the `esports` tag with a Technology or
    # Culture category, so a category-only rule left an Esports sidebar entry
    # that still listed them.
    #
    # Matched against `market_tags.SLUG`, which is Polymarket's own slug — an
    # event is excluded when ANY of its markets carries one of these.
    excluded_tags: list[str] = Field(
        default=["sports", "esports"], validation_alias="AGENTPIT_EXCLUDED_TAGS"
    )
    resolution_mirror_enabled: bool | None = Field(
        default=None, validation_alias="RESOLUTION_MIRROR_ENABLED"
    )
    # How many markets the rotating resolution scan examines per cycle. The
    # scan exists because Polymarket dates short-lived sports markets to the end
    # of the tournament, so `END_DATE < now` never selects them while they are
    # already settled upstream. One upstream fetch per market per cycle, so this
    # is the cost knob: 200 every 5 minutes walks ~2,400 markets in an hour.
    resolution_scan_batch: int = Field(
        default=200, validation_alias="AGENTPIT_RESOLUTION_SCAN_BATCH"
    )
    resolution_mirror_interval_seconds: int = Field(
        default=300, validation_alias="RESOLUTION_MIRROR_INTERVAL_SECONDS"
    )
    auto_redeem_enabled: bool = Field(
        default=True, validation_alias="AUTO_REDEEM_ENABLED"
    )

    # Pinned-series sync (force-sync the current window of recurring markets).
    pinned_series_raw: str = Field(
        default="btc-updown-5m:300", validation_alias="PINNED_SERIES"
    )
    pin_sync_enabled: bool | None = Field(
        default=None, validation_alias="PIN_SYNC_ENABLED"
    )
    pin_sync_offset_seconds: int = Field(
        default=10, validation_alias="PIN_SYNC_OFFSET_SECONDS"
    )
    # How often the live window of each pinned series is re-mirrored from the
    # real Polymarket book. The shared reconciler can take minutes for a full
    # pass over hundreds of markets — far longer than a window's ~5-min life —
    # so the live windows get their own fast loop. With batched placement a
    # re-quote is ~1s, so a 1s interval tracks upstream within ~2s.
    pin_requote_seconds: float = Field(
        default=1.0, validation_alias="AGENTPIT_PIN_REQUOTE_SECONDS"
    )
    # How often just-ended pinned windows are checked for upstream resolution +
    # auto-redeem. The full resolution loop runs every few minutes (fine for
    # long-dated markets), but a 5-min window's winner should be paid within
    # seconds of the upstream market closing, so its windows get their own fast
    # poll. Cheap: scoped to the few most-recently-ended pinned windows.
    pin_resolve_seconds: float = Field(
        default=20.0, validation_alias="AGENTPIT_PIN_RESOLVE_SECONDS"
    )
    # Fast re-quoting cancels + re-places the whole book every second, so the
    # orders table fills with dead 'cancelled' rows. They never slow the live
    # queries (a partial index covers only live rows), but unbounded growth eats
    # disk, so purge cancelled rows older than the retention on a slow loop.
    order_cleanup_interval_seconds: float = Field(
        default=60.0, validation_alias="AGENTPIT_ORDER_CLEANUP_INTERVAL_SECONDS"
    )
    order_cancelled_retention_seconds: int = Field(
        default=600, validation_alias="AGENTPIT_ORDER_CANCELLED_RETENTION_SECONDS"
    )
    idempotency_key_retention_seconds: int = Field(
        default=86400, validation_alias="AGENTPIT_IDEMPOTENCY_KEY_RETENTION_SECONDS"
    )

    @model_validator(mode="after")
    def _default_resolution_mirror_enabled(self) -> "Settings":
        # When RESOLUTION_MIRROR_ENABLED is unset, follow SYNC.
        if self.resolution_mirror_enabled is None:
            self.resolution_mirror_enabled = self.sync_enabled
        return self

    @model_validator(mode="after")
    def _default_pin_sync_enabled(self) -> "Settings":
        # When PIN_SYNC_ENABLED is unset, follow SYNC.
        if self.pin_sync_enabled is None:
            self.pin_sync_enabled = self.sync_enabled
        return self

    @property
    def pinned_series(self) -> list[tuple[str, int]]:
        """Parsed ``[(base, interval), ...]`` from ``PINNED_SERIES``.

        Imported lazily to avoid a config<->polymarket import cycle.
        """
        from agentpit.polymarket.pinned import parse_pinned_series

        return parse_pinned_series(self.pinned_series_raw)

    snapshot_enabled: bool = Field(default=False, validation_alias="SNAPSHOT_ENABLED")
    snapshot_interval_seconds: int = Field(
        default=15 * 60, validation_alias="AGENTPIT_SNAPSHOT_INTERVAL_SECONDS"
    )
    snapshot_retention_days: int = Field(
        default=30, validation_alias="AGENTPIT_SNAPSHOT_RETENTION_DAYS"
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

    # Admin
    admin_token: str = Field(
        default="dev-admin-token",
        validation_alias="AGENTPIT_ADMIN_TOKEN",
    )

    # Liquidity Engine (Phase 5c: Polymarket book mirror)
    liquidity_engine_enabled: bool = Field(
        default=False, validation_alias="LIQUIDITY_ENGINE"
    )
    liquidity_interval_seconds: float = Field(
        default=2.0, validation_alias="AGENTPIT_LIQUIDITY_INTERVAL_SECONDS"
    )
    # ONE mirror account owns every mirror order (spec §6). >1 is unused but
    # kept for provisioning flexibility.
    liquidity_house_account_count: int = Field(
        default=1, validation_alias="AGENTPIT_LIQUIDITY_HOUSE_ACCOUNTS"
    )
    # apUSD is 6-decimal, so every figure here is raw. The house needs ~150bn
    # to seed every mirrored market; 1e18 is headroom chosen deliberately.
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
    # House gas. The mirror signs its own split transactions, so the account
    # spends gas continuously and its signup grant is not a lifetime supply:
    # production burned it in ~82 minutes and the mirror then failed silently.
    # A floor of 5 ETH is ~45 hours of headroom at the observed post-fix rate
    # (0.111 ETH/h), so a refill is never urgent. Topping up is gas ONLY --
    # the zero-balance path in HouseAccountProvisioner means "the chain was
    # reset, re-onboard from scratch" and must stay distinct from this.
    liquidity_gas_floor_wei: int = Field(
        default=5 * 10**18, validation_alias="AGENTPIT_LIQUIDITY_GAS_FLOOR_WEI"
    )
    liquidity_gas_target_wei: int = Field(
        default=100 * 10**18, validation_alias="AGENTPIT_LIQUIDITY_GAS_TARGET_WEI"
    )
    liquidity_gas_check_interval_seconds: float = Field(
        default=300.0,
        validation_alias="AGENTPIT_LIQUIDITY_GAS_CHECK_INTERVAL_SECONDS",
    )
    mirror_assets_per_connection: int = Field(
        default=200, validation_alias="AGENTPIT_MIRROR_ASSETS_PER_CONNECTION"
    )
    mirror_reconcile_min_interval_seconds: float = Field(
        default=0.5, validation_alias="AGENTPIT_MIRROR_RECONCILE_MIN_INTERVAL_SECONDS"
    )
    mirror_watchdog_seconds: float = Field(
        default=120.0, validation_alias="AGENTPIT_MIRROR_WATCHDOG_SECONDS"
    )
    mirror_inventory_buffer: float = Field(
        default=1.2, validation_alias="AGENTPIT_MIRROR_INVENTORY_BUFFER"
    )
    # Headroom minted above the requirement whenever the house is short, in
    # micro-apUSD. The requirement only ever grows, so exact top-ups cost a
    # transaction per upstream depth record; one generous block instead lets a
    # market converge in a single split. 1e14 micro = 100M apUSD, comfortably
    # above the largest requirement observed in production (39M). Collateral is
    # minted freely on the simulated chain, so the block can be this generous —
    # lower it on a chain where collateral costs real money. 0 restores exact
    # top-ups.
    mirror_inventory_seed_micro: int = Field(
        default=100_000_000_000_000,
        validation_alias="AGENTPIT_MIRROR_INVENTORY_SEED_MICRO",
    )
    mirror_max_settlements_per_cycle: int = Field(
        default=1, validation_alias="AGENTPIT_MIRROR_MAX_SETTLEMENTS_PER_CYCLE"
    )
    mirror_tape_enabled: bool = Field(
        default=True, validation_alias="AGENTPIT_MIRROR_TAPE_ENABLED"
    )
    # How often the mirror re-scans the active-market set. Kept short so a new
    # rotating-series window (live for only ~5 min) is picked up and quoted
    # promptly; a no-change scan is a cheap DB read (resubscribe fires only when
    # the set actually changes).
    mirror_target_refresh_seconds: float = Field(
        default=15.0, validation_alias="AGENTPIT_MIRROR_TARGET_REFRESH_SECONDS"
    )
    # Total depth cap per side. The cold sweep converges the local book to this
    # many levels; 0 = unbounded (full 1:1). This is a convergence target, not
    # a hard ceiling: a hot pass never cancels a cold-classified order, so the
    # live count between sweeps can exceed it on a fast-moving market. Each
    # level is ~4 DB order ops, and reconcile_market reads ALL house levels
    # for the market on every hot pass, so hot-path cost tracks that
    # accumulated live set, not mirror_hot_depth.
    mirror_book_depth: int = Field(
        default=8, validation_alias="AGENTPIT_MIRROR_BOOK_DEPTH"
    )
    # Levels per side reconciled on EVERY book update. These carry the price the
    # user sees and the spread a bot trades against, so they must stay live.
    # Everything between this and mirror_book_depth is the cold band, refreshed
    # only by the sweep below. hot == book depth means no cold band at all,
    # which is byte-identical to the pre-two-tier behaviour.
    mirror_hot_depth: int = Field(
        default=8, validation_alias="AGENTPIT_MIRROR_HOT_DEPTH"
    )
    # How often each market's deep levels are reconciled. Deep levels move
    # rarely and nobody trades against them, so this is deliberately slow — it
    # is what keeps a 50-level book from multiplying the hot path.
    mirror_cold_interval_seconds: float = Field(
        default=1800.0, validation_alias="AGENTPIT_MIRROR_COLD_INTERVAL_SECONDS"
    )
