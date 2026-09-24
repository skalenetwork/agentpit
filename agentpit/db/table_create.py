# No global lock: DbSession.read()/write() (session.py) each check out their
# own connection from the pool, so the methods below may run concurrently
# across threads — every one of them operates only on the connection it is
# given.
import json

import psycopg

from agentpit.datastructures.market_state import MarketState


class TableCreate:
    @staticmethod
    def create_trades_table(conn: psycopg.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                TRADE_ID TEXT PRIMARY KEY,
                TAKER_ORDER_ID TEXT,
                MAKER_ORDERS TEXT,
                MARKET TEXT,
                ASSET_ID TEXT,
                PRICE BIGINT,
                TRADE_SIZE BIGINT,
                REMAINING_SIZE BIGINT,
                SIDE TEXT,
                STATUS TEXT,
                MATCH_TIME BIGINT,
                TRANSACTION_HASH TEXT,
                BUCKET_INDEX INTEGER,
                FEE_RATE_BPS BIGINT,
                TAKER_API_KEY TEXT,
                MAKER_API_KEY TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trades_taker_api_key "
            "ON trades(TAKER_API_KEY)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trades_maker_api_key "
            "ON trades(MAKER_API_KEY)"
        )
        # The token the MAKER moved. Equal to ASSET_ID for a NORMAL match, but
        # for a MINT the maker receives the market's other outcome and for a
        # MERGE it burns one — and with only the taker's id recorded, an
        # account's holdings could not be rebuilt from its own trades.
        conn.execute(
            "ALTER TABLE trades ADD COLUMN IF NOT EXISTS MAKER_ASSET_ID TEXT"
        )
        # NORMAL | MINT | MERGE. Derivable from the two sides, but stored so a
        # reader never has to re-derive the matcher's own decision.
        conn.execute(
            "ALTER TABLE trades ADD COLUMN IF NOT EXISTS MATCH_KIND TEXT"
        )
        # scripts/backfill_trade_match_kind.py labels rows written before
        # MATCH_KIND existed, filtering on MATCH_KIND IS NULL each run; this
        # partial index keeps that probe an index scan instead of a full
        # sequential scan that only grows as the table accrues trades. DDL
        # only — the labelling itself does NOT run here (see that script's
        # module docstring for why: it used to, and it blocked app startup).
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trades_unlabelled "
            "ON trades(TRADE_ID) WHERE MATCH_KIND IS NULL"
        )
        # The price tape looks a token up by BOTH columns — the taker's leg on
        # ASSET_ID and, for a MINT/MERGE, the maker's on MAKER_ASSET_ID.
        # Neither was indexed: production measured a 132 ms parallel seq scan
        # over 458k rows to return 21 chart points, on every chart load.
        # idx_trades_asset_id stays full: the taker branch has no MATCH_KIND
        # predicate, so every row is a candidate. idx_trades_maker_asset_id is
        # partial: agentpit/liquidity/tape.py writes MAKER_ASSET_ID = ASSET_ID
        # on every mirrored row, so a full index would carry an entry for all
        # 373k mirror rows that the maker branch — gated on MATCH_KIND IN
        # ('MINT', 'MERGE') — can never return. Measured on a 400k-row
        # mirror-shaped table: full index cost a Bitmap Heap Scan (409
        # buffers); partial costs an Index Scan (208 buffers), and the index
        # itself shrinks from ~3.2 MB to 16 kB.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trades_asset_id ON trades(ASSET_ID)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trades_maker_asset_id "
            "ON trades(MAKER_ASSET_ID) WHERE MATCH_KIND IN ('MINT', 'MERGE')"
        )

    @staticmethod
    def create_orders_table(conn: psycopg.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                API_KEY TEXT,
                PRICE BIGINT,
                POST_ONLY INTEGER,
                ORDER_TYPE TEXT,
                SALT TEXT,
                MAKER TEXT,
                TAKER TEXT,
                SIGNER TEXT,
                TOKEN_ID TEXT,
                MAKER_AMOUNT BIGINT,
                TAKER_AMOUNT BIGINT,
                EXPIRATION BIGINT,
                NONCE INTEGER,
                FEE_RATE_BPS BIGINT,
                SIDE TEXT,
                SIGNATURE_TYPE TEXT,
                SIGNATURE TEXT,
                ORDER_JSON TEXT,
                STATUS TEXT DEFAULT 'live',
                REMAINING_AMOUNT BIGINT,
                CREATED_AT BIGINT,
                ORDER_ID TEXT PRIMARY KEY
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_price_side ON orders(PRICE, SIDE)"
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_orders_order_type_status_expiration
                ON orders(ORDER_TYPE, STATUS, EXPIRATION)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_orders_status_expiration
                ON orders(STATUS, EXPIRATION)
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_api_key ON orders(API_KEY)")
        # The book / price / matcher queries all filter live orders by token; a
        # partial index over just the live rows keeps them index-scans even as
        # the table fills with cancelled rows from fast re-quoting.
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_orders_live_book
                ON orders(TOKEN_ID, SIDE, PRICE) WHERE STATUS = 'live'
            """
        )

    @staticmethod
    def create_users_table(conn: psycopg.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                USER_ID         TEXT PRIMARY KEY,
                EMAIL           TEXT UNIQUE,
                PASSWORD_HASH   TEXT,
                HANDLE          TEXT UNIQUE,
                ETH_ADDRESS     TEXT NOT NULL UNIQUE,
                ETH_PRIVATE_KEY TEXT NOT NULL UNIQUE,
                API_KEY         TEXT NOT NULL UNIQUE,
                ONBOARDED_AT    BIGINT,
                CREATED_AT      BIGINT NOT NULL,
                IS_BOT          INTEGER NOT NULL DEFAULT 0,
                GOOGLE_SUB      TEXT,
                WORKOS_USER_ID  TEXT
            )
            """
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(EMAIL)")
        TableCreate._migrate_users_table(conn)

    @staticmethod
    def _migrate_users_table(conn: psycopg.Connection) -> None:
        """Idempotent additive migration for the users table.

        Pre-auth versions of the schema only had USER_ID, API_KEY, ETH_PRIVATE_KEY.
        Add the new columns if they're missing so existing dev DBs keep working.
        """
        additions = [
            ("EMAIL", "TEXT"),
            ("PASSWORD_HASH", "TEXT"),
            ("HANDLE", "TEXT"),
            ("ETH_ADDRESS", "TEXT"),
            ("ONBOARDED_AT", "BIGINT"),
            ("CREATED_AT", "BIGINT"),
            ("IS_BOT", "INTEGER NOT NULL DEFAULT 0"),
            ("LAST_TOPUP_AT", "BIGINT"),
            ("TOTAL_DEPOSITED", "BIGINT"),
            ("DEPLOYMENT_ID", "TEXT"),
            ("GOOGLE_SUB", "TEXT"),
            ("KEY_EXPORTED_AT", "BIGINT"),
            ("KEY_EXPORT_ATTEMPT_AT", "BIGINT"),
            ("AUTO_REDEEM_ENABLED", "BOOLEAN NOT NULL DEFAULT FALSE"),
            ("WORKOS_USER_ID", "TEXT"),
            ("OWNER_WORKOS_ID", "TEXT"),
            ("AGENT_APP", "TEXT"),
        ]
        for col, col_type in additions:
            conn.execute(
                f"ALTER TABLE users ADD COLUMN IF NOT EXISTS {col} {col_type}"
            )
        # An account that arrived through Google has no password. Databases
        # created before this line have PASSWORD_HASH NOT NULL; dropping it is
        # idempotent, so this is safe on every run.
        conn.execute("ALTER TABLE users ALTER COLUMN PASSWORD_HASH DROP NOT NULL")
        # `sub` is Google's stable id for an account — one of them is one of
        # ours. NULLs do not collide in Postgres, so password-only accounts are
        # unaffected.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google_sub "
            "ON users(GOOGLE_SUB)"
        )
        # The WorkOS `user_...` id, the same way: one of theirs is one of ours.
        # No partial index is needed -- Postgres treats NULLs as distinct in a
        # unique index, so every not-yet-migrated row coexists happily while no
        # two rows can ever share one WorkOS identity.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_workos_user_id "
            "ON users(WORKOS_USER_ID)"
        )
        conn.execute("ALTER TABLE users ALTER COLUMN EMAIL DROP NOT NULL")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_owner_app "
            "ON users(OWNER_WORKOS_ID, AGENT_APP)"
        )

    @staticmethod
    def create_markets_table(conn: psycopg.Connection) -> None:
        allowed_states = ", ".join(f"'{s.value}'" for s in MarketState)
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS markets (
                MARKET_ID BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                CONDITION_ID TEXT NOT NULL UNIQUE,
                POLYMARKET_ID BIGINT,
                POLYMARKET_CONDITION_ID TEXT,
                EVENT_ID BIGINT,
                OUTCOME_LABEL TEXT,
                ICON_URL TEXT,
                POLYMARKET_YES_TOKEN_ID TEXT,
                POLYMARKET_NO_TOKEN_ID TEXT,
                QUESTION TEXT NOT NULL,
                SLUG TEXT NOT NULL,
                DESCRIPTION TEXT NOT NULL,
                ERC1155_TOKENS TEXT NOT NULL,
                START_DATE BIGINT NOT NULL,
                END_DATE BIGINT,
                RESOLVED_OUTCOME INTEGER,
                MARKET_STATE TEXT NOT NULL DEFAULT '{MarketState.DRAFT.value}'
                    CHECK (MARKET_STATE IN ({allowed_states})),
                FULLY_REDEEMED BOOLEAN NOT NULL DEFAULT FALSE
            )
            """
        )
        conn.execute(
            "ALTER TABLE markets ADD COLUMN IF NOT EXISTS POLYMARKET_CONDITION_ID TEXT"
        )
        conn.execute(
            "ALTER TABLE markets ADD COLUMN IF NOT EXISTS EVENT_ID BIGINT"
        )
        conn.execute(
            "ALTER TABLE markets ADD COLUMN IF NOT EXISTS OUTCOME_LABEL TEXT"
        )
        conn.execute(
            "ALTER TABLE markets ADD COLUMN IF NOT EXISTS ICON_URL TEXT"
        )
        conn.execute(
            "ALTER TABLE markets ADD COLUMN IF NOT EXISTS POLYMARKET_YES_TOKEN_ID TEXT"
        )
        conn.execute(
            "ALTER TABLE markets ADD COLUMN IF NOT EXISTS POLYMARKET_NO_TOKEN_ID TEXT"
        )
        conn.execute(
            "ALTER TABLE markets ADD COLUMN IF NOT EXISTS "
            "FULLY_REDEEMED BOOLEAN NOT NULL DEFAULT FALSE"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_markets_condition_id ON markets(CONDITION_ID)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_markets_polymarket_condition_id "
            "ON markets(POLYMARKET_CONDITION_ID)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_markets_event_id ON markets(EVENT_ID)"
        )

    @staticmethod
    def create_market_tags_table(conn: psycopg.Connection) -> None:
        """Polymarket's per-market tag list, mirrored verbatim.

        Tags live on the MARKET, not the event, because that is where Gamma
        puts them and because replacing one market's set on each sync pass is
        self-healing: a tag removed upstream disappears here too. An
        event-level union could only ever grow. An event's tag set is the
        union over its markets, taken by join at read time.

        No foreign key on MARKET_ID — the schema uses plain columns plus
        indexes throughout (markets.EVENT_ID has none either).
        """
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS market_tags (
                MARKET_ID BIGINT NOT NULL,
                SLUG TEXT NOT NULL,
                LABEL TEXT NOT NULL,
                PRIMARY KEY (MARKET_ID, SLUG)
            )
            """
        )
        # The facet and nav queries both start from a slug, so this index is
        # the one that matters; MARKET_ID is already covered by the PK's
        # leading column.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_tags_slug ON market_tags(SLUG)"
        )

    @staticmethod
    def create_events_table(conn: psycopg.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                EVENT_ID BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                SLUG TEXT NOT NULL UNIQUE,
                TITLE TEXT NOT NULL,
                DESCRIPTION TEXT NOT NULL DEFAULT '',
                ICON_URL TEXT,
                CATEGORY TEXT,
                START_DATE BIGINT,
                END_DATE BIGINT,
                POLYMARKET_EVENT_ID TEXT
            )
            """
        )
        # Migration: add upstream 24h volume to existing tables (idempotent, so
        # the running DB upgrades on startup — no reset). Drives homepage order.
        conn.execute(
            "ALTER TABLE events ADD COLUMN IF NOT EXISTS VOLUME_24HR DOUBLE PRECISION"
        )
        # All-time upstream volume, alongside the 24h figure. The cards show
        # this one; the 24h figure still drives the default ordering.
        conn.execute(
            "ALTER TABLE events ADD COLUMN IF NOT EXISTS VOLUME DOUBLE PRECISION"
        )
        # Order-book depth in dollars, straight from Gamma's event payload.
        # Drives the "Liquidity" sort, which until now ranked on the number of
        # outcomes — a different quantity entirely.
        conn.execute(
            "ALTER TABLE events ADD COLUMN IF NOT EXISTS LIQUIDITY DOUBLE PRECISION"
        )
        # How contested the odds are, 0..1. A 50/50 market scores near 1, a
        # 97/3 market near 0. Independent of liquidity: two matches can share a
        # competitive score while their books differ by four orders of magnitude.
        conn.execute(
            "ALTER TABLE events ADD COLUMN IF NOT EXISTS COMPETITIVE DOUBLE PRECISION"
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_events_slug ON events(SLUG)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_polymarket_event_id "
            "ON events(POLYMARKET_EVENT_ID)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_volume_24hr "
            "ON events(VOLUME_24HR)"
        )
        # Dropped, not merely un-created: a plain btree on the column cannot
        # serve `ORDER BY <col> DESC NULLS LAST, EVENT_ID DESC`, so Postgres
        # sorted anyway while every sync UPDATE paid to maintain them. At a
        # couple of thousand events the sort is sub-millisecond regardless.
        conn.execute("DROP INDEX IF EXISTS idx_events_liquidity")
        conn.execute("DROP INDEX IF EXISTS idx_events_competitive")
        # Expression index matching the category filter's LOWER(CATEGORY)
        # predicate in TableRead.list_events_with_markets — a plain btree on
        # CATEGORY would not be usable there.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_category_lower "
            "ON events(LOWER(CATEGORY))"
        )

    @staticmethod
    def create_transactions_table(conn: psycopg.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                TRANSACTION_ID BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                TIMESTAMP timestamptz DEFAULT now(),
                API_KEY TEXT NOT NULL,
                TRANSACTION_TYPE TEXT NOT NULL,
                MARKET_ID BIGINT,
                DETAILS TEXT
            )
            """
        )

    @staticmethod
    def create_agents_table(conn: psycopg.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agents (
                AGENT_ID TEXT PRIMARY KEY,
                PERSONALITY TEXT NOT NULL,
                STATE TEXT NOT NULL DEFAULT '{}',
                HISTORY TEXT NOT NULL DEFAULT '[]',
                TODO TEXT NOT NULL DEFAULT '[]'
            )
            """
        )

    @staticmethod
    def create_personalities_table(conn: psycopg.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS personalities (
                PERSONALITY_ID TEXT PRIMARY KEY,
                PERSONALITY_TITLE TEXT NOT NULL,
                PERSONALITY_SPEC TEXT NOT NULL
            )
            """
        )

    @staticmethod
    def create_price_snapshots_table(conn: psycopg.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS price_snapshots (
                SNAPSHOT_ID BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                MARKET_ID BIGINT NOT NULL,
                ASSET_ID TEXT NOT NULL,
                T BIGINT NOT NULL,
                MID_MICRO_USD BIGINT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_snapshots_market_t "
            "ON price_snapshots(MARKET_ID, T)"
        )

    @staticmethod
    def create_account_snapshots_table(conn: psycopg.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS account_snapshots (
                SNAPSHOT_ID BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
                USER_ID TEXT NOT NULL,
                T BIGINT NOT NULL,
                CAPITAL_RAW BIGINT NOT NULL,
                DEPOSITED_RAW BIGINT NOT NULL
            )
            """
        )
        # Cost basis of the open positions at snapshot time -- what the
        # account actually put to work, as opposed to the grant it was handed.
        # Nullable: rows written before this column existed cannot be
        # reconstructed (the positions have moved since), and they read as 0.
        conn.execute(
            "ALTER TABLE account_snapshots ADD COLUMN IF NOT EXISTS "
            "INVESTED_RAW BIGINT"
        )
        # Mark-to-market gain on those still-open positions. Stored rather than
        # derived because the realized half is the residual: banked profit is
        # (capital - deposited) - this, and there is no other record of where
        # the line between the two falls at snapshot time.
        conn.execute(
            "ALTER TABLE account_snapshots ADD COLUMN IF NOT EXISTS "
            "UNREALIZED_RAW BIGINT"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_account_snapshots_user_t "
            "ON account_snapshots(USER_ID, T DESC)"
        )

    @staticmethod
    def create_idempotency_keys_table(conn: psycopg.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS idempotency_keys (
                API_KEY         TEXT   NOT NULL,
                CLIENT_ORDER_ID TEXT   NOT NULL,
                ORDER_ID        TEXT   NOT NULL,
                CREATED_AT      BIGINT NOT NULL,
                PRIMARY KEY (API_KEY, CLIENT_ORDER_ID)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_idempotency_created_at "
            "ON idempotency_keys(CREATED_AT)"
        )

    @staticmethod
    def create_auth_code_attempts_table(conn: psycopg.Connection) -> None:
        """Fixed-window counters for `POST /auth/code`.

        One row per (rule, subject) pair -- `email:60s:a@b.com`, `ip:1h:1.2.3.4`
        -- rather than a column per rule, so adding a rule costs a constant
        rather than a migration.

        Deliberately NOT on `users`: this endpoint is unauthenticated and the
        subject usually has no row here at all. That is the point of it -- an
        address that never answers must cost us nothing, and a counter hanging
        off a user row cannot count what has no user row.

        Rows are tiny and self-expiring in meaning (a stale WINDOW_START simply
        resets on the next hit), so there is no cleanup job. If the table ever
        grows enough to matter, delete where WINDOW_START is old -- nothing
        reads it.
        """
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS auth_code_attempts (
                BUCKET       TEXT PRIMARY KEY,
                WINDOW_START BIGINT NOT NULL,
                HITS         INTEGER NOT NULL
            )
            """
        )

    @staticmethod
    def create_all_tables(conn: psycopg.Connection) -> None:
        # errors propagate; no exception handling here
        TableCreate.create_orders_table(conn)
        TableCreate.create_trades_table(conn)
        TableCreate.create_users_table(conn)
        TableCreate.create_agents_table(conn)
        TableCreate.create_personalities_table(conn)
        TableCreate.create_events_table(conn)
        TableCreate.create_markets_table(conn)
        TableCreate.create_market_tags_table(conn)
        TableCreate.create_transactions_table(conn)
        TableCreate.create_price_snapshots_table(conn)
        TableCreate.create_account_snapshots_table(conn)
        TableCreate.create_idempotency_keys_table(conn)
        TableCreate.create_auth_code_attempts_table(conn)
