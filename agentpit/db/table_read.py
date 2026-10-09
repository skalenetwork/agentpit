import json
import uuid
from collections.abc import Iterable
from datetime import date
from typing import NamedTuple

import psycopg
from eth_account import Account
from eth_account.signers.local import LocalAccount
from pydantic import BaseModel
from web3 import Web3

from agentpit.utils.parse import parse_32b_hex_private_key
from agentpit.datastructures.board import WireTeam
from agentpit.datastructures.event import Event
from agentpit.datastructures.event_sort import EventSort
from agentpit.datastructures.market import Market
from agentpit.datastructures.market_context import MarketContext
from agentpit.datastructures.market_state import MarketState
from agentpit.datastructures.user import User
from agentpit.domain.sports import SEASON, SERIES_WORDS
from agentpit.liquidity.tape import MIRROR_API_KEY
from ..datastructures.condition_id import ConditionId


class TradedAccount(BaseModel):
    user_id: str
    eth_address: str
    handle: str | None
    app: str | None
    host: str | None


class TradeTally(BaseModel):
    trades: int
    trades_before: int
    first_trade_at: int
    last_trade_at: int


class ActivityDay(BaseModel):
    day: date
    agents: int
    active: int
    trades: int
    volume: int


class DailyClose(BaseModel):
    day: date
    capital: int
    deposited: int


class MarketFields(NamedTuple):
    slug: str
    question: str
    description: str
    end_date: int | None
    icon_url: str | None
    outcome_label: str | None
    price_change_24h: float | None


class EventFields(NamedTuple):
    slug: str
    title: str
    icon_url: str | None
    start_date: int | None
    end_date: int | None
    volume_24hr: float | None
    volume: float | None
    liquidity: float | None
    competitive: float | None
    start_time: int | None
    game_id: str | None
    series_slug: str | None
    category: str | None


class Carried(NamedTuple):
    market_id: int
    pm_condition: str
    state: MarketState
    fields: MarketFields
    tags: frozenset[tuple[str, str]]
    event_id: int | None
    pm_event_id: str | None
    event: EventFields | None


def _excluded_lower(excluded: "Iterable[str] | None") -> "list[str]":
    """Normalise an excluded-category list for SQL: lowercased, blanks dropped.

    Empty result means "exclude nothing", which every caller below turns into
    no predicate at all rather than an always-true one — so the default path
    produces byte-identical SQL to before this existed.
    """
    if not excluded:
        return []
    return sorted({c.strip().lower() for c in excluded if c and c.strip()})


def _tag_excluded_subquery(event_id_expr: str) -> str:
    """EXISTS test: does any market of this event carry an excluded tag?

    The tag graph is the second half of the exclusion. Upstream does not file
    everything sporting under the Sports CATEGORY — two season-winner futures
    and a game-release question sat under Technology/Culture carrying
    `esports` — so a category-only rule left them listed under an Esports
    sidebar entry.
    """
    return (
        "EXISTS (SELECT 1 FROM markets mx JOIN market_tags mtx "
        f"ON mtx.MARKET_ID = mx.MARKET_ID WHERE mx.EVENT_ID = {event_id_expr} "
        "AND mtx.SLUG = ANY(%s))"
    )


def _event_excluded_clause(
    categories: "list[str]", tags: "list[str]"
) -> "tuple[str, list[object]]":
    """`(sql, params)` keeping only events that are in neither list.

    For a query over `events` itself. A NULL CATEGORY must PASS: `LOWER(NULL)
    <> ALL (...)` evaluates to NULL, which WHERE treats as false, so without
    the explicit IS NULL arm every uncategorised event would vanish along with
    the excluded ones.
    """
    parts: list[str] = []
    params: list[object] = []
    if categories:
        parts.append("(CATEGORY IS NULL OR LOWER(CATEGORY) <> ALL(%s))")
        params.append(categories)
    if tags:
        parts.append(f"NOT {_tag_excluded_subquery('events.EVENT_ID')}")
        params.append(tags)
    return " AND ".join(parts), params


def _market_excluded_clause(
    categories: "list[str]", tags: "list[str]", alias: str = "markets"
) -> "tuple[str, list[object]]":
    """`(sql, params)` for a query over `markets`, reaching both signals through
    the event that groups them.

    NOT EXISTS rather than a join: a market with no event, or an event with no
    category and no tags, matches no row in either subquery and is therefore
    KEPT — the same "exclude only on positive evidence" rule the sync filter
    follows.
    """
    parts: list[str] = []
    params: list[object] = []
    if categories:
        parts.append(
            f"NOT EXISTS (SELECT 1 FROM events ev WHERE ev.EVENT_ID = {alias}.EVENT_ID "
            "AND LOWER(ev.CATEGORY) = ANY(%s))"
        )
        params.append(categories)
    if tags:
        parts.append(f"NOT {_tag_excluded_subquery(f'{alias}.EVENT_ID')}")
        params.append(tags)
    return " AND ".join(parts), params


_MARKET_COLS = (
    "MARKET_ID, POLYMARKET_ID, POLYMARKET_CONDITION_ID, CONDITION_ID, QUESTION_ID, "
    "QUESTION, DESCRIPTION, SLUG, "
    "START_DATE, END_DATE, ERC1155_TOKENS, "
    "COALESCE(MARKET_STATE, 'DRAFT') as MARKET_STATE, "
    "PAYOUTS, "
    "EVENT_ID, OUTCOME_LABEL, ICON_URL, "
    "POLYMARKET_YES_TOKEN_ID, POLYMARKET_NO_TOKEN_ID, "
    "COALESCE(FULLY_REDEEMED, FALSE) as FULLY_REDEEMED, "
    "PRICE_CHANGE_24H, RESOLVED_AT"
)


def _row_to_market(row) -> Market:
    erc1155_tokens_json = row["ERC1155_TOKENS"]
    erc1155_tokens = json.loads(erc1155_tokens_json) if erc1155_tokens_json else []
    return Market(
        question=row["QUESTION"],
        market_id=row["MARKET_ID"],
        polymarket_id=row["POLYMARKET_ID"],
        polymarket_condition_id=row["POLYMARKET_CONDITION_ID"],
        polymarket_yes_token_id=row["POLYMARKET_YES_TOKEN_ID"],
        polymarket_no_token_id=row["POLYMARKET_NO_TOKEN_ID"],
        condition_id=ConditionId(row["CONDITION_ID"]),
        question_id=row["QUESTION_ID"],
        description=row["DESCRIPTION"],
        slug=row["SLUG"],
        start_date=row["START_DATE"],
        end_date=row["END_DATE"],
        erc1155_tokens=erc1155_tokens,
        market_state=MarketState(row["MARKET_STATE"]),
        payouts=row["PAYOUTS"],
        event_id=row["EVENT_ID"],
        outcome_label=row["OUTCOME_LABEL"],
        icon_url=row["ICON_URL"],
        fully_redeemed=row["FULLY_REDEEMED"],
        price_change_24h=row["PRICE_CHANGE_24H"],
        resolved_at=row["RESOLVED_AT"],
    )


class TableRead:
    #: Polymarket's grace: a GTD order dies one minute BEFORE its stated
    #: expiration. Their clients compensate by sending `now + 60 + N` for a
    #: lifetime of N, so subtracting it here is what makes "1 hour" an hour.
    #:
    #: Documented, not folklore: Polymarket docs, page `trading/place-orders.mdx`,
    #: "GTD orders expire one minute before their stated expiration as a
    #: security threshold. To set an effective lifetime of N seconds, use
    #: `now + 60 + N`. In addition, the expiration must be at least 3 minutes
    #: in the future — orders expiring sooner are rejected."
    EXPIRY_GRACE_SECONDS = 60

    #: What every read means by a live order. Takes ONE parameter, `now` in
    #: unix seconds, and must be appended at the END of a WHERE clause: these
    #: queries pass positional tuples, so a placeholder inserted mid-clause
    #: silently shifts every parameter after it.
    #:
    #: An expiration of 0 means never — Polymarket's own convention, and the
    #: default on `PlaceOrderRequest`. A NULL expiration reads the same way:
    #: EXPIRATION is a nullable BIGINT with no DEFAULT, so any direct write
    #: that omits it (a test fixture, a future migration) leaves it NULL
    #: rather than 0, and `NULL = 0` / `NULL > x` both evaluate to NULL, which
    #: WHERE treats as false. Without the explicit IS NULL arm such a row
    #: would silently fail every liveness check AND every cancel — an
    #: unkillable ghost order, worse than one that simply never expires.
    LIVE_ORDER = (
        "STATUS = 'live' AND (EXPIRATION IS NULL OR EXPIRATION = 0 "
        f"OR EXPIRATION > %s + {EXPIRY_GRACE_SECONDS})"
    )

    #: One price print per Polymarket print and token: "this token traded at
    #: this price". Only the mirrored tape prints, so nothing an agent does
    #: moves a public price.
    #:
    #: A tape row is a YES print and stores the NO token in MAKER_ASSET_ID, so
    #: the second branch prints NO at MICRO - PRICE on the opposite side. Rows
    #: written before that carry MAKER_ASSET_ID = ASSET_ID and print once.
    #:
    #: Takes TWO parameters, both the SAME list of token ids: the predicate is
    #: pushed into each branch so both use an index. One filter over the union
    #: would seq-scan the whole table.
    #:
    #: Append your own `SELECT ... FROM prints`.
    TOKEN_PRINTS_CTE = """
        WITH prints AS (
            SELECT ASSET_ID AS TOKEN_ID, MATCH_TIME, TRADE_SIZE, PRICE, SIDE
            FROM trades
            WHERE STATUS = 'MIRRORED' AND ASSET_ID = ANY(%s)
            UNION ALL
            SELECT MAKER_ASSET_ID, MATCH_TIME, TRADE_SIZE, 1000000 - PRICE,
                   CASE WHEN SIDE = 'BUY' THEN 'SELL' ELSE 'BUY' END
            FROM trades
            WHERE STATUS = 'MIRRORED' AND MAKER_ASSET_ID = ANY(%s)
              AND MAKER_ASSET_ID <> ASSET_ID
        )
    """

    @staticmethod
    def polymarket_condition_ids(db: psycopg.Connection) -> set[str]:
        rows = db.execute(
            "SELECT POLYMARKET_CONDITION_ID FROM markets "
            "WHERE POLYMARKET_CONDITION_ID IS NOT NULL"
        ).fetchall()
        return {row["POLYMARKET_CONDITION_ID"] for row in rows}

    @staticmethod
    def carried_condition_ids(
        db: psycopg.Connection, condition_ids: list[str]
    ) -> set[str]:
        rows = db.execute(
            "SELECT POLYMARKET_CONDITION_ID FROM markets "
            "WHERE POLYMARKET_CONDITION_ID = ANY(%s)",
            (condition_ids,),
        ).fetchall()
        return {row["POLYMARKET_CONDITION_ID"] for row in rows}

    @staticmethod
    def get_market_state(db: psycopg.Connection, market_id: int) -> MarketState:
        row = db.execute(
            "SELECT MARKET_STATE FROM markets WHERE MARKET_ID = %s", (market_id,)
        ).fetchone()
        return MarketState(row["MARKET_STATE"])

    @staticmethod
    def get_private_key_for_api_key(
        db: psycopg.Connection, api_key: str
    ) -> LocalAccount | None:
        """Return the eth account for an API key, or None if no user matches.

        Read-only — never inserts. Anonymous user creation is gone now that auth
        is required: a request with an unknown api_key resolves to None.
        """
        row = db.execute(
            "SELECT ETH_PRIVATE_KEY FROM users WHERE API_KEY = %s LIMIT 1",
            (api_key,),
        ).fetchone()
        if row is None:
            return None
        existing_key = parse_32b_hex_private_key(row["ETH_PRIVATE_KEY"])
        return Account.from_key(existing_key)

    @staticmethod
    def get_eth_address_for_api_key(db: psycopg.Connection, api_key: str) -> str | None:
        """Return the eth address for an API key, or None if no user matches."""
        row = db.execute(
            "SELECT ETH_ADDRESS FROM users WHERE API_KEY = %s LIMIT 1",
            (api_key,),
        ).fetchone()
        return row["ETH_ADDRESS"] if row else None

    @staticmethod
    def get_user_id_by_api_key(db: psycopg.Connection, api_key: str) -> str | None:
        row = db.execute(
            "SELECT USER_ID FROM users WHERE API_KEY = %s LIMIT 1", (api_key,)
        ).fetchone()
        return row["USER_ID"] if row else None

    @staticmethod
    def get_agent_by_id(db: psycopg.Connection, agent_id: str) -> dict | None:
        """
        Fetch an agent by AGENT_ID.
        Returns a dict with agent_id, personality_id, state, history, todo or None.
        """
        row = db.execute(
            "SELECT PERSONALITY, STATE, HISTORY, TODO FROM agents WHERE AGENT_ID = %s LIMIT 1",
            (agent_id,),
        ).fetchone()

        if row is None:
            return None

        return {
            "agent_id": agent_id,
            "personality_id": row["PERSONALITY"],
            "state": json.loads(row["STATE"]),
            "history": json.loads(row["HISTORY"]),
            "todo": json.loads(row["TODO"]),
        }

    _USER_COLS = (
        "USER_ID, EMAIL, HANDLE, ETH_ADDRESS, ETH_PRIVATE_KEY, "
        "API_KEY, ONBOARDED_AT, CREATED_AT, IS_BOT, WORKOS_USER_ID, AGENT_APP, AGENT_HOST, DELETED_AT, "
        "(PASSWORD_HASH IS NOT NULL) AS HAS_PASSWORD, "
        "(AUTO_REDEEM_ENABLED) AS AUTO_REDEEM"
    )

    @staticmethod
    def _row_to_user(row) -> "User":
        existing_key = parse_32b_hex_private_key(row["ETH_PRIVATE_KEY"])
        acct = Account.from_key(existing_key)
        return User(
            user_id=row["USER_ID"],
            email=row["EMAIL"],
            eth_key=acct,
            eth_address=row["ETH_ADDRESS"],
            api_key=row["API_KEY"],
            handle=row["HANDLE"],
            onboarded_at=row["ONBOARDED_AT"],
            created_at=row["CREATED_AT"] if row["CREATED_AT"] is not None else 0,
            is_bot=bool(row["IS_BOT"]),
            has_password=bool(row["HAS_PASSWORD"]),
            auto_redeem=bool(row["AUTO_REDEEM"]),
            workos_user_id=row["WORKOS_USER_ID"],
            agent_app=row["AGENT_APP"],
            agent_host=row["AGENT_HOST"],
            deleted_at=row["DELETED_AT"],
        )

    @staticmethod
    def get_user_by_userid(db: psycopg.Connection, user_id: str) -> "User | None":
        row = db.execute(
            f"SELECT {TableRead._USER_COLS} FROM users WHERE USER_ID = %s LIMIT 1",
            (user_id,),
        ).fetchone()
        return TableRead._row_to_user(row) if row else None

    @staticmethod
    def get_user_by_api_key(db: psycopg.Connection, api_key: str) -> "User | None":
        row = db.execute(
            f"SELECT {TableRead._USER_COLS} FROM users WHERE API_KEY = %s LIMIT 1",
            (api_key,),
        ).fetchone()
        return TableRead._row_to_user(row) if row else None

    @staticmethod
    def get_agent(
        db: psycopg.Connection, owner_workos_id: str, app: str, client: str | None
    ) -> "User | None":
        row = db.execute(
            f"SELECT {TableRead._USER_COLS} FROM users "
            "WHERE OWNER_WORKOS_ID = %s AND AGENT_APP = %s AND AGENT_CLIENT IS NOT DISTINCT FROM %s "
            "AND DELETED_AT IS NULL",
            (owner_workos_id, app, client),
        ).fetchone()
        return TableRead._row_to_user(row) if row else None

    @staticmethod
    def get_owned_agent(db: psycopg.Connection, owner_workos_id: str, eth_address: str) -> "User | None":
        row = db.execute(
            f"SELECT {TableRead._USER_COLS} FROM users "
            "WHERE OWNER_WORKOS_ID = %s AND ETH_ADDRESS = %s AND DELETED_AT IS NULL",
            (owner_workos_id, eth_address),
        ).fetchone()
        return TableRead._row_to_user(row) if row else None

    @staticmethod
    def agents_owned_by(db: psycopg.Connection, owner_workos_id: str) -> list[User]:
        rows = db.execute(
            f"SELECT {TableRead._USER_COLS} FROM users "
            "WHERE OWNER_WORKOS_ID = %s AND DELETED_AT IS NULL ORDER BY CREATED_AT, AGENT_APP, USER_ID",
            (owner_workos_id,),
        ).fetchall()
        return [TableRead._row_to_user(r) for r in rows]

    @staticmethod
    def get_idempotency_order_id(
        db: psycopg.Connection, api_key: str, client_order_id: str
    ) -> "str | None":
        row = db.execute(
            "SELECT ORDER_ID FROM idempotency_keys "
            "WHERE API_KEY = %s AND CLIENT_ORDER_ID = %s",
            (api_key, client_order_id),
        ).fetchone()
        return row["ORDER_ID"] if row else None

    @staticmethod
    def get_user_by_email(db: psycopg.Connection, email: str) -> "User | None":
        row = db.execute(
            f"SELECT {TableRead._USER_COLS} FROM users WHERE EMAIL = %s LIMIT 1",
            (email,),
        ).fetchone()
        return TableRead._row_to_user(row) if row else None

    @staticmethod
    def get_user_by_google_sub(
        db: psycopg.Connection, google_sub: str
    ) -> "User | None":
        row = db.execute(
            f"SELECT {TableRead._USER_COLS} FROM users WHERE GOOGLE_SUB = %s LIMIT 1",
            (google_sub,),
        ).fetchone()
        return TableRead._row_to_user(row) if row else None

    @staticmethod
    def get_user_by_workos_id(
        db: psycopg.Connection, workos_user_id: str
    ) -> "User | None":
        """The account this WorkOS identity belongs to, matched exactly.

        Deliberately not case-insensitive, unlike `get_user_by_email_ci`: an
        address is something a person types and gets wrong, while this is an
        opaque id we stored ourselves.
        """
        row = db.execute(
            f"SELECT {TableRead._USER_COLS} FROM users WHERE WORKOS_USER_ID = %s",
            (workos_user_id,),
        ).fetchone()
        return TableRead._row_to_user(row) if row else None

    @staticmethod
    def get_user_by_email_ci(db: psycopg.Connection, email: str) -> "User | None":
        """Case-insensitive email lookup, used only for linking a Google identity.

        Registration stores the address as typed, so `Alice@Example.com` and the
        `alice@example.com` Google reports are the same person to everyone
        except `=`. Linking is the one place that difference would mint a second
        wallet, so it is the one place that compares case-insensitively. Login
        keeps the exact-match reader above.
        """
        row = db.execute(
            f"SELECT {TableRead._USER_COLS} FROM users "
            "WHERE LOWER(EMAIL) = LOWER(%s) ORDER BY CREATED_AT LIMIT 1",
            (email,),
        ).fetchone()
        return TableRead._row_to_user(row) if row else None

    @staticmethod
    def handle_taken(db: psycopg.Connection, handle: str) -> bool:
        """Whether a handle is already claimed.

        `HANDLE TEXT UNIQUE` is the real guarantee; this is what lets
        registration pick a different name instead of surfacing a constraint
        violation as a 500.
        """
        row = db.execute(
            "SELECT 1 FROM users WHERE HANDLE = %s", (handle,)
        ).fetchone()
        return row is not None

    @staticmethod
    def get_user_by_eth_address(db: psycopg.Connection, eth_address: str) -> "User | None":
        row = db.execute(
            f"SELECT {TableRead._USER_COLS} FROM users WHERE ETH_ADDRESS = %s LIMIT 1",
            (eth_address,),
        ).fetchone()
        return TableRead._row_to_user(row) if row else None

    @staticmethod
    def get_password_hash_by_userid(db: psycopg.Connection, user_id: str) -> str | None:
        row = db.execute(
            "SELECT PASSWORD_HASH FROM users WHERE USER_ID = %s LIMIT 1",
            (user_id,),
        ).fetchone()
        return row["PASSWORD_HASH"] if row else None

    @staticmethod
    def get_key_export_state(
        db: psycopg.Connection, user_id: str
    ) -> "tuple[int | None, int | None]":
        """`(exported_at, last_attempt_at)` for one user, epoch seconds."""
        row = db.execute(
            "SELECT KEY_EXPORTED_AT, KEY_EXPORT_ATTEMPT_AT FROM users "
            "WHERE USER_ID = %s",
            (user_id,),
        ).fetchone()
        if row is None:
            return (None, None)
        exported = row["KEY_EXPORTED_AT"]
        attempted = row["KEY_EXPORT_ATTEMPT_AT"]
        return (
            int(exported) if exported is not None else None,
            int(attempted) if attempted is not None else None,
        )

    @staticmethod
    def get_last_topup_at(db: psycopg.Connection, user_id: str) -> int | None:
        row = db.execute(
            "SELECT LAST_TOPUP_AT FROM users WHERE USER_ID = %s", (user_id,)
        ).fetchone()
        return row["LAST_TOPUP_AT"] if row else None

    @staticmethod
    def get_total_deposited(
        db: psycopg.Connection, user_id: str, default_raw: int
    ) -> int:
        """Raw apUSD this account has been handed, grant included.

        NULL means the row predates the column. It reads as `default_raw` --
        the signup grant -- because a backfill migration could only have
        written the same number, and doing it at the read keeps the two
        production accounts and the house correct without one.
        """
        row = db.execute(
            "SELECT TOTAL_DEPOSITED FROM users WHERE USER_ID = %s", (user_id,)
        ).fetchone()
        if row is None or row["TOTAL_DEPOSITED"] is None:
            return default_raw
        return int(row["TOTAL_DEPOSITED"])

    @staticmethod
    def get_deployment_id(db: psycopg.Connection, user_id: str) -> str | None:
        """Which chain deployment this account's figures were recorded against.

        NULL means the row predates the column; the caller records the current
        identity without resetting, because it has no evidence a wipe happened.
        """
        row = db.execute(
            "SELECT DEPLOYMENT_ID FROM users WHERE USER_ID = %s", (user_id,)
        ).fetchone()
        return row["DEPLOYMENT_ID"] if row else None

    #: Exposed as a constant so `tests/db/test_traded_accounts_plan.py` can
    #: EXPLAIN the query that actually runs, rather than a copy of it that
    #: would drift.
    #:
    #: `<> %s` on the api key is the mirror tape, and it is the difference
    #: between 3.2 seconds and 1.4 milliseconds. Semantically it is nothing:
    #: `MIRROR_API_KEY` is "opaque, never a real user's api key"
    #: (`agentpit/liquidity/tape.py`), so no row it excludes could have
    #: matched `u.API_KEY` anyway. To the planner it is everything: that one
    #: value is 99.76% of `trades`, and without excluding it the estimate for
    #: "rows matching this api key" is ~87,000 instead of 0, which makes an
    #: `EXISTS` look like it will stop on the first row and a sequential scan
    #: look nearly free. It does not stop -- the accounts being probed have no
    #: trades at all -- so each probe reads all 523,000 rows, thirty times per
    #: request. See the test for the full measurement.
    TRADED_ACCOUNTS_SQL = """
            SELECT u.USER_ID, u.ETH_ADDRESS, u.HANDLE, u.AGENT_APP AS APP, u.AGENT_HOST AS HOST
            FROM users u
            WHERE u.IS_BOT = 0
              AND u.DELETED_AT IS NULL
              AND (
                EXISTS (
                    SELECT 1 FROM trades t
                    WHERE t.TAKER_API_KEY = u.API_KEY AND t.STATUS != %s
                      AND t.TAKER_API_KEY <> %s
                )
                OR EXISTS (
                    SELECT 1 FROM trades t
                    WHERE t.MAKER_API_KEY = u.API_KEY AND t.STATUS != %s
                      AND t.MAKER_API_KEY <> %s
                )
              )
            ORDER BY u.USER_ID
            """

    @staticmethod
    def list_traded_accounts(db: psycopg.Connection) -> "list[TradedAccount]":
        """Every non-house account with at least one non-failed trade, taker
        or maker.

        Having traded is the membership rule: it keeps every registered
        address off a public board by default, and an account that never
        traded has nothing to rank. The house is excluded because it is the
        counterparty to nearly every trade rather than a competitor. A
        `FAILED` trade doesn't count -- it never settled, matching every
        other trade reader in the codebase (account_service.py,
        order_service.py).

        A previous version of this query joined `users` to a `UNION ALL`
        over the two api-key columns. That fixed a worse nested-loop plan
        (measured at 1318ms) but still scanned `trades` in full on every
        call -- and `trades` is dominated by the liquidity mirror's
        synthetic tape, a row per WSS last-trade event across ~1000 books
        with no retention, so the scan grows without bound in a quantity
        that has nothing to do with how many accounts have traded.

        This drives from `users` instead (tens of rows, not millions) and
        asks per user whether a matching trade exists, so each user resolves
        to an index probe on `idx_trades_taker_api_key` /
        `idx_trades_maker_api_key` that stops at the first hit rather than a
        scan of the whole table. No `DISTINCT` is needed: `users` is the only
        table in `FROM`, so a self-matched trade (taker == maker) can only
        make one of the two `EXISTS` clauses true for that user once, not
        twice. Measured locally with `EXPLAIN (ANALYZE, TIMING OFF)` against
        300k synthetic tape rows + 2k real trades across 40 accounts: the old
        form 60.2ms (`Hash Join` over two `Seq Scan`s of `trades`), this form
        0.06ms (`Index Scan` per user on `idx_trades_taker_api_key` /
        `idx_trades_maker_api_key`, second `EXISTS` short-circuited by the
        first on every row in this sample).
        """
        rows = db.execute(
            TableRead.TRADED_ACCOUNTS_SQL,
            ("FAILED", MIRROR_API_KEY, "FAILED", MIRROR_API_KEY),
        ).fetchall()
        return [TradedAccount.model_validate(r) for r in rows]

    @staticmethod
    def count_trades_by_user(db: psycopg.Connection, before: int) -> "dict[str, TradeTally]":
        """user_id -> how many non-failed trades it took part in, either side,
        how many of them matched before `before`, and when the first and the
        latest of them matched.

        Drives from `users` for the same reason as `list_traded_accounts`:
        `trades` is dominated by the liquidity mirror's unbounded synthetic
        tape, and a query with no predicate on it re-scans that tape in full
        on every call regardless of how many accounts have actually traded.
        Pushing `IS_BOT = 0` into a `UNION ALL` join still left the plan
        linear in tape size (measured 167ms -> 32ms, better but not
        index-driven).

        This form correlates a `LATERAL` subquery per user instead, so each
        user resolves to an index probe rather than a share of a full-table
        scan. `COUNT(DISTINCT x.TRADE_ID)` -- not `COUNT(*)` -- is still
        required: the `UNION ALL` inside the lateral emits one row per
        api-key column, so a trade where this user is both taker and maker
        appears twice within that user's own subquery and must be
        collapsed back to one. Measured locally the same way as
        `list_traded_accounts`: the old form 58.6ms (`Hash Join` over two
        `Seq Scan`s of `trades`), this form 1.4ms (`Nested Loop` driven by
        `users`, `Index Scan` on both api-key indexes per user).
        """
        rows = db.execute(
            """
            SELECT u.USER_ID AS uid,
                   COUNT(DISTINCT x.TRADE_ID) AS trades,
                   COUNT(DISTINCT x.TRADE_ID) FILTER (WHERE x.MATCH_TIME < %(before)s) AS trades_before,
                   MIN(x.MATCH_TIME) AS first_trade_at,
                   MAX(x.MATCH_TIME) AS last_trade_at
            FROM users u
            JOIN LATERAL (
                SELECT TRADE_ID, MATCH_TIME FROM trades
                WHERE TAKER_API_KEY = u.API_KEY AND STATUS != 'FAILED'
                UNION ALL
                SELECT TRADE_ID, MATCH_TIME FROM trades
                WHERE MAKER_API_KEY = u.API_KEY AND STATUS != 'FAILED'
            ) x ON true
            WHERE u.IS_BOT = 0
            GROUP BY u.USER_ID
            """,
            {"before": before},
        ).fetchall()
        return {r["uid"]: TradeTally.model_validate(r) for r in rows}

    @staticmethod
    def latest_account_snapshots(
        db: psycopg.Connection,
    ) -> "dict[str, tuple[int, int, int, int]]":
        """user_id -> (capital, deposited, invested, unrealized), newest row.

        INVESTED_RAW and UNREALIZED_RAW are NULL on rows written before those
        columns existed; both read as 0 rather than being backfilled, because
        the positions they valued have moved since and any backfill would be a
        guess.
        """
        rows = db.execute(
            """
            SELECT DISTINCT ON (USER_ID)
                   USER_ID, CAPITAL_RAW, DEPOSITED_RAW, INVESTED_RAW,
                   UNREALIZED_RAW
            FROM account_snapshots
            ORDER BY USER_ID, T DESC, SNAPSHOT_ID DESC
            """
        ).fetchall()
        return {
            r["USER_ID"]: (
                int(r["CAPITAL_RAW"]),
                int(r["DEPOSITED_RAW"]),
                int(r["INVESTED_RAW"] or 0),
                int(r["UNREALIZED_RAW"] or 0),
            )
            for r in rows
        }

    @staticmethod
    def daily_activity(db: psycopg.Connection, today: date) -> "list[ActivityDay]":
        """Per UTC day from the first trade to `today`: agents counted from
        their first trade, agents that traded that day, and trades with an
        agent on either side and their paper notional at the taker's price,
        each counted once. A failed trade never counts."""
        rows = db.execute(
            """
            WITH fills AS (
                SELECT u.USER_ID, x.TRADE_ID, x.MATCH_TIME / 86400 AS DAY,
                       (CASE WHEN COALESCE(x.MATCH_KIND, 'NORMAL') IN ('MINT', 'MERGE')
                             THEN 1000000 - x.PRICE ELSE x.PRICE END)
                       * x.TRADE_SIZE / 1000000 AS NOTIONAL
                FROM users u
                CROSS JOIN LATERAL (
                    SELECT TRADE_ID, MATCH_TIME, MATCH_KIND, PRICE, TRADE_SIZE FROM trades
                    WHERE TAKER_API_KEY = u.API_KEY AND STATUS <> 'FAILED'
                    UNION ALL
                    SELECT TRADE_ID, MATCH_TIME, MATCH_KIND, PRICE, TRADE_SIZE FROM trades
                    WHERE MAKER_API_KEY = u.API_KEY AND STATUS <> 'FAILED'
                ) x
                WHERE u.IS_BOT = 0
            ),
            firsts AS (
                SELECT MIN(DAY) AS DAY FROM fills GROUP BY USER_ID
            ),
            matches AS (
                SELECT DISTINCT TRADE_ID, DAY, NOTIONAL FROM fills
            )
            SELECT DATE 'epoch' + d.DAY::int AS day,
                   (SELECT COUNT(*) FROM firsts f WHERE f.DAY <= d.DAY) AS agents,
                   COUNT(DISTINCT fl.USER_ID) AS active,
                   COUNT(DISTINCT fl.TRADE_ID) AS trades,
                   (SELECT COALESCE(SUM(m.NOTIONAL), 0) FROM matches m WHERE m.DAY = d.DAY) AS volume
            FROM generate_series(
                (SELECT MIN(DAY) FROM firsts), %(today)s::date - DATE 'epoch'
            ) AS d(DAY)
            LEFT JOIN fills fl ON fl.DAY = d.DAY
            GROUP BY d.DAY
            ORDER BY d.DAY
            """,
            {"today": today},
        ).fetchall()
        return [ActivityDay.model_validate(r) for r in rows]

    @staticmethod
    def daily_closes(
        db: psycopg.Connection, user_ids: "list[str]", since: date
    ) -> "dict[str, list[DailyClose]]":
        """user_id -> each UTC day's last valuation from `since` on, oldest
        first; a day without one is absent."""
        closes: dict[str, list[DailyClose]] = {}
        for r in db.execute(
            """
            SELECT DISTINCT ON (USER_ID, T / 86400)
                   USER_ID AS user_id,
                   DATE 'epoch' + (T / 86400)::int AS day,
                   CAPITAL_RAW AS capital,
                   DEPOSITED_RAW AS deposited
            FROM account_snapshots
            WHERE USER_ID = ANY(%(users)s)
              AND T >= (%(since)s::date - DATE 'epoch')::bigint * 86400
            ORDER BY USER_ID, T / 86400, T DESC, SNAPSHOT_ID DESC
            """,
            {"users": user_ids, "since": since},
        ):
            closes.setdefault(r["user_id"], []).append(DailyClose.model_validate(r))
        return closes

    @staticmethod
    def read_market(db: psycopg.Connection, market_id: int) -> "Market | None":
        row = db.execute(
            f"SELECT {_MARKET_COLS} FROM markets WHERE MARKET_ID = %s",
            (market_id,),
        ).fetchone()
        return _row_to_market(row) if row else None

    @staticmethod
    def read_market_by_condition_id(
        db: psycopg.Connection, condition_id: ConditionId
    ) -> "Market | None":
        row = db.execute(
            f"SELECT {_MARKET_COLS} FROM markets WHERE CONDITION_ID = %s",
            (condition_id.value,),
        ).fetchone()
        return _row_to_market(row) if row else None

    @staticmethod
    def list_all_markets(db: psycopg.Connection) -> "list[Market]":
        cur = db.execute(f"SELECT {_MARKET_COLS} FROM markets ORDER BY MARKET_ID")
        return [_row_to_market(row) for row in cur.fetchall()]

    @staticmethod
    def count_open_markets(
        db: psycopg.Connection,
        sided: "list[str]",
        excluded_categories: "Iterable[str] | None" = None,
        excluded_tags: "Iterable[str] | None" = None,
    ) -> int:
        """How many markets are open: ACTIVE with a two-sided book, the pool MCP
        `search_markets` reads.

        `excluded_categories` must be the SAME list the grid's query gets, or the
        headline counts markets the grid refuses to show.
        """
        sql, extra = _market_excluded_clause(
            _excluded_lower(excluded_categories), _excluded_lower(excluded_tags)
        )
        clause = f" AND {sql}" if sql else ""
        params: list[object] = [MarketState.ACTIVE.value, sided, *extra]
        row = db.execute(
            "SELECT COUNT(*) as CNT FROM markets WHERE MARKET_STATE = %s "
            f"AND ERC1155_TOKENS::jsonb->0->>0 = ANY(%s){clause}",
            tuple(params),
        ).fetchone()
        return int(row["CNT"]) if row else 0

    @staticmethod
    def list_markets(
        db: psycopg.Connection, limit: int = 100, offset: int = 0
    ) -> "tuple[list[Market], int]":
        total = db.execute("SELECT COUNT(*) as CNT FROM markets").fetchone()["CNT"]
        cur = db.execute(
            f"SELECT {_MARKET_COLS} FROM markets "
            "ORDER BY MARKET_ID DESC LIMIT %s OFFSET %s",
            (limit, offset),
        )
        markets = [_row_to_market(row) for row in cur.fetchall()]
        return markets, total

    @staticmethod
    def list_markets_with_user_activity(
        db: psycopg.Connection, api_key: str
    ) -> "list[Market]":
        """Markets where `api_key` has any trade or split/merge transaction —
        the only markets where the user can hold a CTF balance (you only acquire
        outcome tokens via a fill or a split). Lets list_positions scan just
        these (usually a handful) on-chain instead of every market, which is
        O(market count) sequential on-chain reads."""
        cur = db.execute(
            f"SELECT {_MARKET_COLS} FROM markets WHERE CONDITION_ID IN "
            "(SELECT MARKET FROM trades "
            " WHERE TAKER_API_KEY = %s OR MAKER_API_KEY = %s) "
            "OR MARKET_ID IN (SELECT MARKET_ID FROM transactions WHERE API_KEY = %s)",
            (api_key, api_key, api_key),
        )
        return [_row_to_market(row) for row in cur.fetchall()]

    _EVENT_COLS = (
        "EVENT_ID, SLUG, TITLE, DESCRIPTION, ICON_URL, CATEGORY, "
        "START_DATE, END_DATE, POLYMARKET_EVENT_ID, VOLUME_24HR, VOLUME, "
        "LIQUIDITY, COMPETITIVE, START_TIME, GAME_ID, SERIES_SLUG"
    )

    @staticmethod
    def _row_to_event(row) -> "Event":
        return Event(
            event_id=row["EVENT_ID"],
            slug=row["SLUG"],
            title=row["TITLE"],
            description=row["DESCRIPTION"] or "",
            icon_url=row["ICON_URL"],
            category=row["CATEGORY"],
            start_date=row["START_DATE"],
            end_date=row["END_DATE"],
            polymarket_event_id=row["POLYMARKET_EVENT_ID"],
            volume_24hr=row["VOLUME_24HR"],
            volume=row["VOLUME"],
            liquidity=row["LIQUIDITY"],
            competitive=row["COMPETITIVE"],
            start_time=row["START_TIME"],
            game_id=row["GAME_ID"],
            series_slug=row["SERIES_SLUG"],
        )

    @staticmethod
    def get_event_by_id(db: psycopg.Connection, event_id: int) -> "Event | None":
        row = db.execute(
            f"SELECT {TableRead._EVENT_COLS} FROM events WHERE EVENT_ID = %s LIMIT 1",
            (event_id,),
        ).fetchone()
        return TableRead._row_to_event(row) if row else None

    @staticmethod
    def event_slugs_by_id(
        db: psycopg.Connection, event_ids: "list[int]"
    ) -> "dict[int, str]":
        """``{event_id: slug}`` for the ids that exist, in one query.

        The account reads need an event slug per market so the profile can link
        a position at the event that groups it rather than at the bare market.
        Fetching each one through ``get_event_by_id`` would be a query per row.
        """
        wanted = sorted({int(e) for e in event_ids if e is not None})
        if not wanted:
            return {}
        cur = db.execute(
            "SELECT EVENT_ID, SLUG FROM events WHERE EVENT_ID = ANY(%s)", (wanted,)
        )
        return {int(r["EVENT_ID"]): str(r["SLUG"]) for r in cur.fetchall()}

    @staticmethod
    def categories_by_condition_id(db: psycopg.Connection, condition_ids: "list[str]") -> "dict[str, str]":
        rows = db.execute(
            "SELECT m.CONDITION_ID, e.CATEGORY FROM markets m JOIN events e ON e.EVENT_ID = m.EVENT_ID "
            "WHERE m.CONDITION_ID = ANY(%s) AND e.CATEGORY IS NOT NULL",
            (condition_ids,),
        ).fetchall()
        return {str(r["CONDITION_ID"]): str(r["CATEGORY"]) for r in rows}

    @staticmethod
    def kickoffs_by_condition_id(db: psycopg.Connection, condition_ids: "list[str]") -> "dict[str, int]":
        rows = db.execute(
            "SELECT m.CONDITION_ID, e.START_TIME FROM markets m JOIN events e ON e.EVENT_ID = m.EVENT_ID "
            "JOIN market_tags t ON t.MARKET_ID = m.MARKET_ID AND t.SLUG = 'games' "
            "WHERE m.CONDITION_ID = ANY(%s) AND e.START_TIME IS NOT NULL",
            (condition_ids,),
        ).fetchall()
        return {str(r["CONDITION_ID"]): r["START_TIME"] for r in rows}

    @staticmethod
    def market_contexts(
        db: psycopg.Connection, condition_ids: "list[str]", sided: "list[str]"
    ) -> "dict[str, MarketContext]":
        """The event title only when the event groups several markets and reads
        differently; the winner is the only paid outcome's label."""
        rows = db.execute(
            "SELECT m.CONDITION_ID, m.END_DATE, m.RESOLVED_AT, m.ERC1155_TOKENS::jsonb -> 0 ->> 0 AS YES, "
            "m.ERC1155_TOKENS::jsonb -> (2 - array_position(m.PAYOUTS, 0)) ->> 1 AS WINNER, "
            "CASE WHEN e.TITLE <> m.QUESTION AND (SELECT COUNT(*) FROM markets s WHERE s.EVENT_ID = m.EVENT_ID) > 1 "
            "THEN e.TITLE END AS EVENT_TITLE "
            "FROM markets m LEFT JOIN events e ON e.EVENT_ID = m.EVENT_ID WHERE m.CONDITION_ID = ANY(%s)",
            (condition_ids,),
        ).fetchall()
        kickoffs = TableRead.kickoffs_by_condition_id(db, condition_ids)
        open_books = set(sided)
        return {
            str(r["CONDITION_ID"]): MarketContext(
                eventTitle=r["EVENT_TITLE"], endDate=r["END_DATE"], resolvedAt=r["RESOLVED_AT"], winner=r["WINNER"],
                kickoff=(kickoff := kickoffs.get(r["CONDITION_ID"])),
                trading=None if kickoff is None else r["YES"] in open_books,
            )
            for r in rows
        }

    @staticmethod
    def get_event_by_slug(db: psycopg.Connection, slug: str) -> "Event | None":
        row = db.execute(
            f"SELECT {TableRead._EVENT_COLS} FROM events WHERE SLUG = %s LIMIT 1",
            (slug,),
        ).fetchone()
        return TableRead._row_to_event(row) if row else None

    @staticmethod
    def get_event_by_polymarket_event_id(
        db: psycopg.Connection, polymarket_event_id: str
    ) -> "Event | None":
        row = db.execute(
            f"SELECT {TableRead._EVENT_COLS} FROM events "
            "WHERE POLYMARKET_EVENT_ID = %s LIMIT 1",
            (polymarket_event_id,),
        ).fetchone()
        return TableRead._row_to_event(row) if row else None

    @staticmethod
    def board_events(
        db: psycopg.Connection,
        *,
        settled_since: int,
        excluded_categories: "Iterable[str] | None",
        excluded_tags: "Iterable[str] | None",
    ) -> "list[tuple[Event, list[Market]]]":
        excl_sql, excl_params = _event_excluded_clause(
            _excluded_lower(excluded_categories), _excluded_lower(excluded_tags)
        )
        clauses = [
            "EXISTS (SELECT 1 FROM markets m WHERE m.EVENT_ID = events.EVENT_ID "
            "AND (m.MARKET_STATE = %s OR (m.MARKET_STATE = %s "
            "AND COALESCE(m.RESOLVED_AT, m.END_DATE) >= %s "
            f"AND NOT {_tag_excluded_subquery('events.EVENT_ID')})))"
        ]
        params: list[object] = [
            MarketState.ACTIVE.value,
            MarketState.RESOLVED.value,
            settled_since,
            ["up-or-down"],
        ]
        if excl_sql:
            clauses.append(excl_sql)
            params.extend(excl_params)
        events = [
            TableRead._row_to_event(r)
            for r in db.execute(
                f"SELECT {TableRead._EVENT_COLS} FROM events "
                f"WHERE {' AND '.join(clauses)} ORDER BY EVENT_ID",
                tuple(params),
            ).fetchall()
        ]
        markets: dict[int, list[Market]] = {e.event_id: [] for e in events}
        for row in db.execute(
            f"SELECT {_MARKET_COLS} FROM markets "
            "WHERE EVENT_ID = ANY(%s) AND MARKET_STATE <> %s ORDER BY MARKET_ID",
            (list(markets), MarketState.CANCELLED.value),
        ).fetchall():
            markets[int(row["EVENT_ID"])].append(_row_to_market(row))
        return [(e, markets[e.event_id]) for e in events]

    @staticmethod
    def tag_slugs_by_event(
        db: psycopg.Connection, event_ids: "list[int]"
    ) -> "dict[int, set[str]]":
        out: dict[int, set[str]] = {}
        for r in db.execute(
            "SELECT DISTINCT m.EVENT_ID, t.SLUG FROM market_tags t "
            "JOIN markets m ON m.MARKET_ID = t.MARKET_ID WHERE m.EVENT_ID = ANY(%s)",
            (event_ids,),
        ).fetchall():
            out.setdefault(int(r["EVENT_ID"]), set()).add(str(r["SLUG"]))
        return out

    @staticmethod
    def games_without_teams(
        db: psycopg.Connection,
    ) -> "dict[str, tuple[int, list[str]]]":
        markets: dict[str, tuple[int, list[tuple[list[str], str]]]] = {}
        for r in db.execute(
            "SELECT e.EVENT_ID, e.POLYMARKET_EVENT_ID, m.ERC1155_TOKENS, "
            "COALESCE(m.OUTCOME_LABEL, m.QUESTION) AS LABEL "
            "FROM events e JOIN markets m ON m.EVENT_ID = e.EVENT_ID "
            "WHERE e.GAME_ID IS NOT NULL AND e.POLYMARKET_EVENT_ID IS NOT NULL "
            "AND m.MARKET_STATE = %s "
            "AND NOT EXISTS (SELECT 1 FROM outcome_teams t WHERE t.EVENT_ID = e.EVENT_ID) "
            "ORDER BY m.MARKET_ID",
            (MarketState.ACTIVE.value,),
        ).fetchall():
            _, ms = markets.setdefault(
                r["POLYMARKET_EVENT_ID"], (int(r["EVENT_ID"]), [])
            )
            ms.append(
                ([label for _, label in json.loads(r["ERC1155_TOKENS"])], r["LABEL"])
            )
        return {
            pm: (event_id, ms[0][0] if len(ms) == 1 else [label for _, label in ms])
            for pm, (event_id, ms) in markets.items()
        }

    @staticmethod
    def outcome_teams(
        db: psycopg.Connection, event_ids: "list[int]"
    ) -> "dict[tuple[int, str], WireTeam]":
        return {
            (int(r["EVENT_ID"]), r["LABEL"]): WireTeam(
                logo=r["LOGO"], record=r["RECORD"], color=r["COLOR"], abbr=r["ABBR"]
            )
            for r in db.execute(
                "SELECT EVENT_ID, LABEL, LOGO, RECORD, COLOR, ABBR FROM outcome_teams "
                "WHERE EVENT_ID = ANY(%s) AND COALESCE(LOGO, RECORD, COLOR, ABBR) IS NOT NULL",
                (event_ids,),
            ).fetchall()
        }

    @staticmethod
    def list_markets_by_event_id(db: psycopg.Connection, event_id: int) -> "list[Market]":
        cur = db.execute(
            f"SELECT {_MARKET_COLS} FROM markets "
            "WHERE EVENT_ID = %s ORDER BY MARKET_ID",
            (event_id,),
        )
        return [_row_to_market(row) for row in cur.fetchall()]

    @staticmethod
    def list_events_with_markets(
        db: psycopg.Connection,
        limit: int = 100,
        offset: int = 0,
        category: str | None = None,
        tag: str | None = None,
        subtags: "list[str] | None" = None,
        sort: "EventSort | None" = None,
        excluded_categories: "Iterable[str] | None" = None,
        excluded_tags: "Iterable[str] | None" = None,
    ) -> "tuple[list[tuple[Event, list[Market]]], int]":
        """Return events ranked by upstream 24h volume, each paired with its
        child markets.

        Used by the home page: every market belongs to an event, so this is
        the primary listing query. Events with a captured upstream
        ``VOLUME_24HR`` rank first (descending); events never synced from
        upstream (NULL volume — orphan singletons, not-yet-refreshed rows) fall
        to the bottom, ordered newest-first as a stable tiebreak. One query for
        the event page + one for all member markets (bucketed in Python) — no
        N+1.

        When ``category`` is given (blank/whitespace counts as absent) the page
        is restricted to that category case-insensitively, so a case drift
        between the label the UI sends and the label stored can't silently
        return an empty page. ``total`` reflects the same filter.

        ``tag`` and ``subtags`` filter on the tag graph rather than the
        CATEGORY column: an event matches when any of its markets carries the
        slug. They compose with each other and with ``category`` as AND, while
        ``subtags`` ORs within itself. Blank and whitespace-only values count
        as absent, exactly as ``category`` does.

        ``sort`` chooses the ordering; ``None`` means
        ``EventSort.DEFAULT`` — 24h volume, the ranking the home page has used
        since before sorting was a choice. Every clause ends in ``EVENT_ID
        DESC`` so equal values cannot swap between pages, and puts missing
        values last so a never-captured event never leads the list.

        ``EventSort.ENDING_SOON`` additionally restricts the page to events
        that have not already ended (see the predicate below): ascending
        order over the whole catalogue would otherwise lead with events that
        ended months ago, since a never-ending stream of past events sorts
        before every future one. No other sort is restricted — a stale event
        still belongs in "Newest" or "Total Volume".
        """
        # Predicates accumulate and are joined with AND; the tag filters are
        # EXISTS subqueries because an event's tag set lives on its markets.
        # `subtags` ORs within itself via ANY() while still ANDing against
        # `tag` — a facet like `trump` also occurs outside `politics`, and
        # dropping the parent would let a Politics > Trump selection surface a
        # non-Politics event.
        resolved_sort = sort or EventSort.DEFAULT
        clauses: list[str] = []
        params: list[object] = []
        # Applied before the caller's own `category` filter, and independent of
        # it: a request for an excluded category returns an empty page rather
        # than resurrecting it, so a stale UI tab cannot reach the rows.
        excl_sql, excl_params = _event_excluded_clause(
            _excluded_lower(excluded_categories), _excluded_lower(excluded_tags)
        )
        if excl_sql:
            clauses.append(excl_sql)
            params.extend(excl_params)
        normalized_category = category.strip() if category else None
        if normalized_category:
            clauses.append("LOWER(CATEGORY) = LOWER(%s)")
            params.append(normalized_category)
        normalized_tag = tag.strip().lower() if tag else None
        if normalized_tag:
            clauses.append(
                "EXISTS (SELECT 1 FROM markets m "
                "JOIN market_tags mt ON mt.MARKET_ID = m.MARKET_ID "
                "WHERE m.EVENT_ID = events.EVENT_ID AND mt.SLUG = %s)"
            )
            params.append(normalized_tag)
        normalized_subtags = [
            s.strip().lower() for s in (subtags or []) if s and s.strip()
        ]
        if normalized_subtags:
            clauses.append(
                "EXISTS (SELECT 1 FROM markets m "
                "JOIN market_tags mt ON mt.MARKET_ID = m.MARKET_ID "
                "WHERE m.EVENT_ID = events.EVENT_ID AND mt.SLUG = ANY(%s))"
            )
            params.append(normalized_subtags)
        if resolved_sort is EventSort.ENDING_SOON:
            # Scoped to this one sort: "Ending Soon" is the only ordering an
            # already-ended event would otherwise lead, because ASC over the
            # whole catalogue puts every past END_DATE ahead of every future
            # one. `EXTRACT(EPOCH FROM NOW())` runs in Postgres rather than
            # being passed in as a parameter, so the cutoff is the database's
            # clock and the query needs no extra binding.
            #
            # NULL END_DATE passes the filter (treated as "not ended", not
            # excluded): a missing end date is not evidence the event is
            # over, and ORDER BY's NULLS LAST already keeps it at the bottom
            # of the page rather than the top, exactly as it did before this
            # predicate existed.
            clauses.append("(END_DATE IS NULL OR END_DATE >= EXTRACT(EPOCH FROM NOW()))")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

        total = db.execute(
            f"SELECT COUNT(*) as CNT FROM events{where}",
            tuple(params),
        ).fetchone()["CNT"]
        events_cur = db.execute(
            f"SELECT {TableRead._EVENT_COLS} FROM events{where} "
            f"ORDER BY {resolved_sort.order_by()} "
            "LIMIT %s OFFSET %s",
            tuple(params + [limit, offset]),
        )
        events = [TableRead._row_to_event(r) for r in events_cur.fetchall()]
        if not events:
            return [], total

        ids = [ev.event_id for ev in events]
        placeholders = ",".join(["%s"] * len(ids))
        markets_cur = db.execute(
            f"SELECT {_MARKET_COLS} FROM markets "
            f"WHERE EVENT_ID IN ({placeholders}) ORDER BY EVENT_ID, MARKET_ID",
            ids,
        )
        by_event: dict[int, list[Market]] = {eid: [] for eid in ids}
        for row in markets_cur.fetchall():
            market = _row_to_market(row)
            assert market.event_id is not None  # guaranteed by WHERE clause
            by_event[market.event_id].append(market)
        return [(ev, by_event[ev.event_id]) for ev in events], total

    @staticmethod
    def list_markets_filtered(
        db: psycopg.Connection,
        *,
        limit: int = 100,
        offset: int = 0,
        market_id: int | None = None,
        slug: str | None = None,
        condition_ids: list[str] | None = None,
        clob_token_ids: list[str] | None = None,
        polymarket_condition_id: str | None = None,
        excluded_categories: "Iterable[str] | None" = None,
        excluded_tags: "Iterable[str] | None" = None,
    ) -> "list[Market]":
        """Paged/filtered market list.

        `excluded_categories` hides whole categories from BROWSING. It is a
        parameter rather than a fixed rule because the direct lookups that also
        come through here are addressing a known market, not browsing, and must
        keep resolving it.
        """
        clauses: list[str] = []
        params: list = []
        excl_sql, excl_params = _market_excluded_clause(
            _excluded_lower(excluded_categories), _excluded_lower(excluded_tags)
        )
        if excl_sql:
            clauses.append(excl_sql)
            params.extend(excl_params)
        if market_id is not None:
            clauses.append("MARKET_ID = %s")
            params.append(market_id)
        if slug is not None:
            clauses.append("SLUG = %s")
            params.append(slug)
        if condition_ids:
            placeholders = ",".join("%s" for _ in condition_ids)
            clauses.append(f"CONDITION_ID IN ({placeholders})")
            params.extend(condition_ids)
        if polymarket_condition_id is not None:
            clauses.append("POLYMARKET_CONDITION_ID = %s")
            params.append(polymarket_condition_id)
        if clob_token_ids:
            # Match markets whose ERC1155_TOKENS JSON contains any given token id.
            # Quote-anchored, wildcards escaped (see resolve.resolve_by_token_id).
            ors = []
            for token_id in clob_token_ids:
                escaped = (
                    token_id.replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                ors.append("ERC1155_TOKENS LIKE %s ESCAPE '\\'")
                params.append(f'%"{escaped}"%')
            clauses.append("(" + " OR ".join(ors) + ")")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        cur = db.execute(
            f"SELECT {_MARKET_COLS} FROM markets {where} "
            "ORDER BY MARKET_ID DESC LIMIT %s OFFSET %s",
            (*params, limit, offset),
        )
        return [_row_to_market(row) for row in cur.fetchall()]

    @staticmethod
    def list_event_categories(
        db: psycopg.Connection,
        excluded_categories: "Iterable[str] | None" = None,
        excluded_tags: "Iterable[str] | None" = None,
    ) -> "list[str]":
        """Every distinct, non-blank event category, case-insensitively sorted.

        Postgres rejects ``SELECT DISTINCT ... ORDER BY <expr>`` when the
        expression is not in the select list, so the DISTINCT happens in a
        subquery. ``COLLATE "C"`` is the explicit tiebreak that keeps the order
        total (and "Sports" before "sports") whatever the server's lc_collate.

        This is what the UI builds its category tabs from, so it MUST honour the
        same exclusions the event grid does. Leaving an excluded category in the
        list renders a tab whose every click returns an empty page.
        """
        excl_sql, excl_params = _event_excluded_clause(
            _excluded_lower(excluded_categories), _excluded_lower(excluded_tags)
        )
        clause = f" AND {excl_sql}" if excl_sql else ""
        params: tuple = tuple(excl_params)
        cur = db.execute(
            f"""
            SELECT c FROM (
                SELECT DISTINCT CATEGORY AS c
                FROM events
                WHERE CATEGORY IS NOT NULL AND TRIM(CATEGORY) <> ''{clause}
            ) s
            ORDER BY LOWER(c) ASC, c COLLATE "C" ASC
            """,
            params,
        )
        return [str(row["c"]) for row in cur.fetchall()]

    @staticmethod
    def list_tag_nav(
        db: psycopg.Connection,
        *,
        slugs: list[str],
        min_events: int,
        excluded_categories: "Iterable[str] | None" = None,
        excluded_tags: "Iterable[str] | None" = None,
    ) -> "list[tuple[str, str, int]]":
        """``(slug, label, event_count)`` for each requested slug that carries
        at least ``min_events`` events. Unordered — the caller restores the
        curated order.

        Counts DISTINCT events, not tag rows: one event whose two markets both
        carry `politics` is one Politics event, not two. Markets with no event
        are excluded — an unbound market is not reachable from any listing.

        ``MIN(LABEL)`` rather than an arbitrary pick: after an upstream rename
        the same slug can briefly carry two labels across markets, and the
        answer must not flicker between calls.

        The count must honour `excluded_categories` for the same reason the
        category list does, and this is the surface that actually renders the
        sidebar: excluded events left in the count kept a "Sports 2035" entry
        whose every click returned an empty grid. Counting them out drops the
        slug below `min_events` on its own, so no separate deny-list is needed
        and a tag that survives on non-excluded events keeps its place.
        """
        if not slugs:
            return []
        excl_sql, excl_params = _market_excluded_clause(
            _excluded_lower(excluded_categories), _excluded_lower(excluded_tags), "m"
        )
        clause = f" AND {excl_sql}" if excl_sql else ""
        params: list[object] = [list(slugs), *excl_params, min_events]
        cur = db.execute(
            f"""
            SELECT mt.SLUG AS SLUG, MIN(mt.LABEL) AS LABEL,
                   COUNT(DISTINCT m.EVENT_ID) AS CNT
            FROM market_tags mt
            JOIN markets m ON m.MARKET_ID = mt.MARKET_ID
            WHERE m.EVENT_ID IS NOT NULL AND mt.SLUG = ANY(%s){clause}
            GROUP BY mt.SLUG
            HAVING COUNT(DISTINCT m.EVENT_ID) >= %s
            """,
            tuple(params),
        )
        return [(str(r["SLUG"]), str(r["LABEL"]), int(r["CNT"])) for r in cur.fetchall()]

    @staticmethod
    def list_tag_facets(
        db: psycopg.Connection,
        *,
        parent_slug: str,
        blocked: "frozenset[str]",
        deprecated_prefix: str,
        limit: int,
        max_coverage: float,
    ) -> "list[tuple[str, str, int]]":
        """Tags co-occurring with ``parent_slug``, most common first.

        "Co-occurring" is at EVENT level: a facet counts an event whose tag
        union contains both slugs, even when they arrived on different markets
        of that event.

        Two filters run in Python rather than SQL because both need the
        parent's own total, which the same pass computes: the coverage ceiling
        (a facet matching nearly every event of its parent is the parent under
        another name) and the length cap.
        """
        parent_total = db.execute(
            """
            SELECT COUNT(DISTINCT m.EVENT_ID) AS CNT
            FROM market_tags mt
            JOIN markets m ON m.MARKET_ID = mt.MARKET_ID
            WHERE mt.SLUG = %s AND m.EVENT_ID IS NOT NULL
            """,
            (parent_slug,),
        ).fetchone()["CNT"]
        if not parent_total:
            return []
        cur = db.execute(
            """
            WITH parent_events AS (
                SELECT DISTINCT m.EVENT_ID AS EVENT_ID
                FROM market_tags mt
                JOIN markets m ON m.MARKET_ID = mt.MARKET_ID
                WHERE mt.SLUG = %s AND m.EVENT_ID IS NOT NULL
            )
            SELECT mt.SLUG AS SLUG, MIN(mt.LABEL) AS LABEL,
                   COUNT(DISTINCT m.EVENT_ID) AS CNT
            FROM market_tags mt
            JOIN markets m ON m.MARKET_ID = mt.MARKET_ID
            JOIN parent_events pe ON pe.EVENT_ID = m.EVENT_ID
            WHERE mt.SLUG <> %s
              AND NOT (mt.SLUG = ANY(%s))
              AND mt.SLUG NOT LIKE %s
            GROUP BY mt.SLUG
            ORDER BY CNT DESC, mt.SLUG ASC
            """,
            (parent_slug, parent_slug, list(blocked), f"{deprecated_prefix}%"),
        )
        out: list[tuple[str, str, int]] = []
        for row in cur.fetchall():
            count = int(row["CNT"])
            if count / parent_total > max_coverage:
                continue
            out.append((str(row["SLUG"]), str(row["LABEL"]), count))
            if len(out) >= limit:
                break
        return out

    @staticmethod
    def list_orphan_markets(db: psycopg.Connection) -> "list[Market]":
        """Markets with no EVENT_ID — used by the auto-wrap singleton helper."""
        cur = db.execute(
            f"SELECT {_MARKET_COLS} FROM markets "
            "WHERE EVENT_ID IS NULL ORDER BY MARKET_ID"
        )
        return [_row_to_market(row) for row in cur.fetchall()]

    @staticmethod
    def list_active_synced_markets(
        db: psycopg.Connection,
        excluded_categories: "Iterable[str] | None" = None,
        excluded_tags: "Iterable[str] | None" = None,
    ) -> "list[Market]":
        """Markets the house quotes from Polymarket's book.

        Criteria: MARKET_STATE = 'ACTIVE' AND POLYMARKET_CONDITION_ID IS NOT NULL,
        minus any market in an excluded category or tag.

        Returned busiest first, so the feed subscribes them in that order.
        Volume lives on the event, so it is read through a correlated subquery
        rather than a join: `_MARKET_COLS` is unqualified and a join would make
        every column name ambiguous. Markets whose event has no captured volume
        sort last, then by id, so the order is total and stable across restarts.
        """
        sql, extra = _market_excluded_clause(
            _excluded_lower(excluded_categories), _excluded_lower(excluded_tags)
        )
        clause = f" AND {sql}" if sql else ""
        params: tuple = tuple(extra)
        rows = db.execute(
            f"SELECT {_MARKET_COLS} FROM markets "
            "WHERE MARKET_STATE = 'ACTIVE' AND POLYMARKET_CONDITION_ID IS NOT NULL"
            f"{clause} "
            "ORDER BY (SELECT e.VOLUME_24HR FROM events e "
            "          WHERE e.EVENT_ID = markets.EVENT_ID) DESC NULLS LAST, "
            "         MARKET_ID",
            params,
        ).fetchall()
        return [_row_to_market(row) for row in rows]

    @staticmethod
    def list_carried(db: psycopg.Connection) -> "list[Carried]":
        rows = db.execute(
            "SELECT m.MARKET_ID, m.POLYMARKET_CONDITION_ID, m.MARKET_STATE, "
            "m.SLUG, m.QUESTION, m.DESCRIPTION, m.END_DATE, m.ICON_URL, m.OUTCOME_LABEL, "
            "m.PRICE_CHANGE_24H, m.EVENT_ID, e.POLYMARKET_EVENT_ID, e.TITLE AS E_TITLE, "
            "e.ICON_URL AS E_ICON_URL, e.START_DATE AS E_START_DATE, e.END_DATE AS E_END_DATE, "
            "e.VOLUME_24HR AS E_VOLUME_24HR, e.VOLUME AS E_VOLUME, e.LIQUIDITY AS E_LIQUIDITY, "
            "e.COMPETITIVE AS E_COMPETITIVE, e.START_TIME AS E_START_TIME, e.SLUG AS E_SLUG, "
            "e.GAME_ID AS E_GAME_ID, e.SERIES_SLUG AS E_SERIES_SLUG, e.CATEGORY AS E_CATEGORY, "
            "(SELECT array_agg(ARRAY[t.SLUG, t.LABEL]) FROM market_tags t "
            "WHERE t.MARKET_ID = m.MARKET_ID) AS TAGS "
            "FROM markets m LEFT JOIN events e ON e.EVENT_ID = m.EVENT_ID "
            "WHERE m.MARKET_STATE IN ('ACTIVE', 'CLOSED') "
            "AND m.POLYMARKET_CONDITION_ID IS NOT NULL"
        ).fetchall()
        return [
            Carried(
                market_id=r["MARKET_ID"],
                pm_condition=r["POLYMARKET_CONDITION_ID"],
                state=MarketState(r["MARKET_STATE"]),
                fields=MarketFields(*(r[c] for c in MarketFields._fields)),
                tags=frozenset((slug, label) for slug, label in r["TAGS"] or ()),
                event_id=r["EVENT_ID"],
                pm_event_id=r["POLYMARKET_EVENT_ID"],
                event=(
                    EventFields(*(r[f"e_{c}"] for c in EventFields._fields))
                    if r["e_title"] is not None
                    else None
                ),
            )
            for r in rows
        ]

    @staticmethod
    def ended_unresolved(
        db: psycopg.Connection, since: int, until: int, limit: int
    ) -> "dict[str, int]":
        rows = db.execute(
            "SELECT POLYMARKET_CONDITION_ID, MARKET_ID FROM markets "
            "WHERE MARKET_STATE IN ('ACTIVE', 'CLOSED') "
            "AND POLYMARKET_CONDITION_ID IS NOT NULL "
            "AND END_DATE BETWEEN %s AND %s "
            "ORDER BY END_DATE DESC LIMIT %s",
            (since, until, limit),
        ).fetchall()
        return {r["POLYMARKET_CONDITION_ID"]: r["MARKET_ID"] for r in rows}

    @staticmethod
    def search_live_markets(
        db: psycopg.Connection,
        *,
        query: str | None,
        limit: int,
        sided: list[str],
        excluded_categories: "Iterable[str] | None" = None,
        excluded_tags: "Iterable[str] | None" = None,
    ) -> "list[Market]":
        clauses = [
            "MARKET_STATE = 'ACTIVE'",
            "ERC1155_TOKENS::jsonb->0->>0 = ANY(%s)",
        ]
        params: list[object] = [sided]
        if query:
            clauses.append(
                "to_tsvector('english', concat_ws(' ', QUESTION, "
                "(SELECT concat_ws(' ', e.TITLE, e.CATEGORY, e.SERIES_SLUG, "
                "%s::jsonb ->> regexp_replace(e.SERIES_SLUG, %s, '')) "
                "FROM events e WHERE e.EVENT_ID = markets.EVENT_ID))) "
                "@@ websearch_to_tsquery('english', %s)"
            )
            params += [json.dumps(SERIES_WORDS), SEASON.pattern, query]
        excl_sql, excl_params = _market_excluded_clause(
            _excluded_lower(excluded_categories), _excluded_lower(excluded_tags)
        )
        if excl_sql:
            clauses.append(excl_sql)
            params += excl_params
        rows = db.execute(
            f"SELECT {_MARKET_COLS} FROM markets WHERE {' AND '.join(clauses)} "
            "ORDER BY (SELECT e.VOLUME_24HR FROM events e WHERE e.EVENT_ID = markets.EVENT_ID) "
            "DESC NULLS LAST, MARKET_ID DESC LIMIT %s",
            (*params, limit),
        ).fetchall()
        return [_row_to_market(r) for r in rows]

    @staticmethod
    def last_trade_prices_for_tokens(
        db: psycopg.Connection, token_ids: "list[str]"
    ) -> "dict[str, int]":
        """Most-recent price print per token, batched.

        Reads prints rather than raw rows: a YES print also prints NO at
        MICRO - p.
        """
        if not token_ids:
            return {}
        ids = list(token_ids)
        rows = db.execute(
            TableRead.TOKEN_PRINTS_CTE
            + "SELECT DISTINCT ON (TOKEN_ID) TOKEN_ID, PRICE FROM prints "
              "ORDER BY TOKEN_ID, MATCH_TIME DESC",
            (ids, ids),
        ).fetchall()
        return {r["TOKEN_ID"]: int(r["PRICE"]) for r in rows}

    @staticmethod
    def list_resolved_unredeemed_markets(
        db: psycopg.Connection, limit: int
    ) -> "list[Market]":
        """Auto-redeem candidates: RESOLVED and not yet fully redeemed."""
        rows = db.execute(
            f"SELECT {_MARKET_COLS} FROM markets "
            "WHERE MARKET_STATE = 'RESOLVED' "
            "AND COALESCE(FULLY_REDEEMED, FALSE) = FALSE "
            "ORDER BY MARKET_ID DESC LIMIT %s",
            (limit,),
        ).fetchall()
        return [_row_to_market(row) for row in rows]

    @staticmethod
    def list_participant_api_keys_for_market(
        db: psycopg.Connection, market_id: int, token_ids: "list[str]"
    ) -> "set[str]":
        """Distinct api_keys that traded the market's tokens or split/merged it.

        Scopes the auto-redeem holder scan to real participants (including the
        house/mirror bot account, which appears as a trade maker).
        """
        keys: set[str] = set()
        if token_ids:
            placeholders = ",".join("%s" for _ in token_ids)
            rows = db.execute(
                f"SELECT TAKER_API_KEY, MAKER_API_KEY FROM trades "
                f"WHERE ASSET_ID IN ({placeholders})",
                token_ids,
            ).fetchall()
            for r in rows:
                if r["TAKER_API_KEY"]:
                    keys.add(r["TAKER_API_KEY"])
                if r["MAKER_API_KEY"]:
                    keys.add(r["MAKER_API_KEY"])
        rows = db.execute(
            "SELECT DISTINCT API_KEY FROM transactions "
            "WHERE MARKET_ID = %s AND TRANSACTION_TYPE IN ('SPLIT', 'MERGE')",
            (market_id,),
        ).fetchall()
        for r in rows:
            if r["API_KEY"]:
                keys.add(r["API_KEY"])
        return keys

    @staticmethod
    def list_trades_for_api_key(
        db: psycopg.Connection,
        api_key: str,
        *,
        market: str | None = None,
        asset_id: str | None = None,
        trade_id: str | None = None,
        taker_order_id: str | None = None,
        before: int | None = None,
        after: int | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Trades where the user is taker OR maker, newest first.

        `limit`, when given, is applied in SQL (ORDER BY MATCH_TIME DESC
        already makes the ordering deterministic, so this returns the same
        page a Python-side `[:limit]` slice would) instead of building every
        matching row only to throw most of them away.
        """
        clauses = ["(TAKER_API_KEY = %s OR MAKER_API_KEY = %s)"]
        params: list = [api_key, api_key]
        if market is not None:
            clauses.append("MARKET = %s"); params.append(market)
        if asset_id is not None:
            # A MINT/MERGE maker's own token is MAKER_ASSET_ID, not ASSET_ID
            # (that's the taker's), and filtering on ASSET_ID alone dropped
            # their fill entirely. But the match must stay per-leg: a flat
            # OR of both asset columns would also match api_key's row via
            # the COUNTERPARTY's leg, not just their own.
            clauses.append(
                "((TAKER_API_KEY = %s AND ASSET_ID = %s) OR "
                "(MAKER_API_KEY = %s AND COALESCE(MAKER_ASSET_ID, ASSET_ID) = %s))"
            )
            params.extend([api_key, asset_id, api_key, asset_id])
        if trade_id is not None:
            clauses.append("TRADE_ID = %s"); params.append(trade_id)
        if taker_order_id is not None:
            clauses.append("TAKER_ORDER_ID = %s"); params.append(taker_order_id)
        if before is not None:
            clauses.append("MATCH_TIME < %s"); params.append(before)
        if after is not None:
            clauses.append("MATCH_TIME > %s"); params.append(after)
        query = (
            "SELECT TRADE_ID, TAKER_ORDER_ID, MAKER_ORDERS, MARKET, ASSET_ID, "
            "MAKER_ASSET_ID, MATCH_KIND, "
            "PRICE, TRADE_SIZE, SIDE, STATUS, MATCH_TIME, TRANSACTION_HASH, "
            "BUCKET_INDEX, FEE_RATE_BPS, TAKER_API_KEY, MAKER_API_KEY "
            f"FROM trades WHERE {' AND '.join(clauses)} ORDER BY MATCH_TIME DESC"
        )
        if limit is not None:
            query += " LIMIT %s"
            params.append(limit)
        cur = db.execute(query, params)
        # Keep the case-insensitive dict rows — a plain dict(r) would lower-case
        # the keys and break the upper-case access in TradeService.
        return list(cur.fetchall())

    @staticmethod
    def pending_settlements(
        db: psycopg.Connection, before: int
    ) -> list[tuple[str, int, list[str]]]:
        rows = db.execute(
            "SELECT TRANSACTION_HASH, MIN(MATCH_TIME) AS MATCH_TIME, "
            "ARRAY_AGG(TRADE_ID) AS TRADE_IDS FROM trades "
            "WHERE STATUS = 'PENDING' AND TRANSACTION_HASH <> '' AND MATCH_TIME < %s "
            "GROUP BY TRANSACTION_HASH",
            (before,),
        ).fetchall()
        return [(r["TRANSACTION_HASH"], r["MATCH_TIME"], r["TRADE_IDS"]) for r in rows]

    @staticmethod
    def unsent_settlements(db: psycopg.Connection, before: int) -> list[dict]:
        return db.execute(
            "SELECT t.TRADE_ID, o.ORDER_JSON FROM trades t "
            "JOIN orders o ON o.ORDER_ID = (t.MAKER_ORDERS::jsonb -> 0 ->> 'order_id') "
            "WHERE t.STATUS = 'PENDING' AND t.TRANSACTION_HASH IS NULL AND t.MATCH_TIME < %s",
            (before,),
        ).fetchall()

    @staticmethod
    def get_transaction_history(db: psycopg.Connection, api_key: str) -> list:
        """
        Fetch the transaction history for a given API key.
        """
        cursor = db.execute(
            """
            SELECT TRANSACTION_ID, TIMESTAMP, TRANSACTION_TYPE, MARKET_ID, DETAILS
            FROM transactions
            WHERE API_KEY = %s
            ORDER BY TIMESTAMP DESC
            """,
            (api_key,),
        )
        transactions = []
        for row in cursor.fetchall():
            transactions.append(
                {
                    "transaction_id": row["TRANSACTION_ID"],
                    "timestamp": row["TIMESTAMP"],
                    "transaction_type": row["TRANSACTION_TYPE"],
                    "market_id": row["MARKET_ID"],
                    "details": json.loads(row["DETAILS"]) if row["DETAILS"] else {},
                }
            )
        return transactions
