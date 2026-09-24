import json
import time as _time
import uuid
import psycopg
from web3 import Web3
from eth_account import Account
from eth_account.signers.local import LocalAccount

from agentpit.common import check_state
from agentpit.datastructures.condition_id import ConditionId
from agentpit.datastructures.create_market_request import CreateMarketRequest
from agentpit.datastructures.event import Event
from agentpit.datastructures.market import Market
from agentpit.datastructures.market_state import MarketState
from agentpit.db.table_read import TableRead


class TableWrite:
    @staticmethod
    def create_user(
        db: psycopg.Connection,
        email: str | None,
        password_hash: str | None,
        handle: str | None = None,
        google_sub: str | None = None,
        owner_workos_id: str | None = None,
        agent_app: str | None = None,
    ) -> tuple[str, LocalAccount, str]:
        """Create a new user with an auto-generated eth keypair.

        Returns (user_id, eth_account, api_key). The caller is responsible for
        running on-chain onboarding (faucet drip + approvals) and then calling
        :func:`mark_user_onboarded` once those txns confirm.
        """
        acct: LocalAccount = Account.create()
        key_hex: str = Web3.to_hex(acct.key)
        user_id: str = str(uuid.uuid4())
        api_key: str = str(uuid.uuid4())
        created_at = int(_time.time())

        db.execute(
            """
            INSERT INTO users (
                USER_ID, EMAIL, PASSWORD_HASH, HANDLE,
                ETH_ADDRESS, ETH_PRIVATE_KEY, API_KEY,
                ONBOARDED_AT, CREATED_AT, GOOGLE_SUB,
                OWNER_WORKOS_ID, AGENT_APP
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, NULL, %s, %s, %s, %s)
            """,
            (
                user_id,
                email,
                password_hash,
                handle,
                acct.address,
                key_hex,
                api_key,
                created_at,
                google_sub,
                owner_workos_id,
                agent_app,
            ),
        )
        return user_id, acct, api_key

    @staticmethod
    def mark_user_onboarded(db: psycopg.Connection, user_id: str) -> None:
        db.execute(
            "UPDATE users SET ONBOARDED_AT = %s WHERE USER_ID = %s",
            (int(_time.time()), user_id),
        )

    @staticmethod
    def clear_user_onboarded(db: psycopg.Connection, user_id: str) -> bool:
        """Test-only: put a row back into the never-onboarded state.

        The condition it recreates is real -- `_create_account` commits the row
        before onboarding it, so a chain outage leaves exactly this -- but
        nothing in the product ever writes it, and the repair paths that read
        `ONBOARDED_AT` cannot be tested without a way to produce it.
        """
        cur = db.execute(
            "UPDATE users SET ONBOARDED_AT = NULL WHERE USER_ID = %s", (user_id,)
        )
        return cur.rowcount > 0

    @staticmethod
    def update_user_handle(db: psycopg.Connection, user_id: str, handle: str) -> bool:
        cur = db.execute(
            "UPDATE users SET HANDLE = %s WHERE USER_ID = %s",
            (handle, user_id),
        )
        return cur.rowcount > 0

    @staticmethod
    def update_user_password_hash(
        db: psycopg.Connection, user_id: str, password_hash: str
    ) -> bool:
        cur = db.execute(
            "UPDATE users SET PASSWORD_HASH = %s WHERE USER_ID = %s",
            (password_hash, user_id),
        )
        return cur.rowcount > 0

    @staticmethod
    def set_auto_redeem(
        db: psycopg.Connection, user_id: str, enabled: bool
    ) -> bool:
        cur = db.execute(
            "UPDATE users SET AUTO_REDEEM_ENABLED = %s WHERE USER_ID = %s",
            (enabled, user_id),
        )
        return cur.rowcount > 0

    @staticmethod
    def claim_auth_code_attempt(
        db: psycopg.Connection,
        bucket: str,
        at: int,
        window_seconds: int,
        limit: int,
    ) -> bool:
        """Spend one hit against a fixed window. False when the window is full.

        The same idiom as `mark_key_export_attempt` below and for the same
        reason: the predicate and the increment are ONE statement, so two
        concurrent callers cannot both read the same stale count, both find it
        under the limit, and both proceed. Postgres serialises them on the row
        lock; the loser re-evaluates its `WHERE` against the count the winner
        just committed.

        Fixed window, not sliding: a caller who spends the whole allowance in
        the last second of a window gets a fresh one immediately after, so the
        true worst case is twice the limit across a window boundary. Accepted --
        the rule exists to stop a flood and a bill, not to be a precise quota,
        and a sliding window costs a row per request instead of a row per
        subject.

        A bucket nobody has hit yet takes the INSERT path and is always allowed.
        """
        expired_before = at - window_seconds
        cur = db.execute(
            """
            INSERT INTO auth_code_attempts (BUCKET, WINDOW_START, HITS)
            VALUES (%(bucket)s, %(at)s, 1)
            ON CONFLICT (BUCKET) DO UPDATE SET
                WINDOW_START = CASE
                    WHEN auth_code_attempts.WINDOW_START <= %(expired)s
                    THEN %(at)s ELSE auth_code_attempts.WINDOW_START END,
                HITS = CASE
                    WHEN auth_code_attempts.WINDOW_START <= %(expired)s
                    THEN 1 ELSE auth_code_attempts.HITS + 1 END
            WHERE auth_code_attempts.WINDOW_START <= %(expired)s
               OR auth_code_attempts.HITS < %(limit)s
            """,
            {
                "bucket": bucket,
                "at": at,
                "expired": expired_before,
                "limit": limit,
            },
        )
        return cur.rowcount > 0

    @staticmethod
    def mark_key_export_attempt(
        db: psycopg.Connection, user_id: str, at: int, not_before: int
    ) -> bool:
        """Claim the export-attempt cooldown, atomically.

        The predicate and the stamp are one statement -- the same idiom as
        `claim_topup` -- so two concurrent callers cannot both read the same
        stale `KEY_EXPORT_ATTEMPT_AT` and both pass the check before either
        writes. Under READ COMMITTED, a second UPDATE that targets a row
        another open transaction is about to write blocks on that row's
        lock; once the first commits, the second re-evaluates its WHERE
        clause against the value that commit just wrote, not the value it
        started with. So the loser sees the winner's fresh stamp and its own
        predicate fails, returning `rowcount == 0` here -- the cooldown is
        active precisely because someone just claimed it, not because of a
        stale read.

        Returns False when the cooldown is still active.
        """
        cur = db.execute(
            "UPDATE users SET KEY_EXPORT_ATTEMPT_AT = %s "
            "WHERE USER_ID = %s "
            "AND (KEY_EXPORT_ATTEMPT_AT IS NULL OR KEY_EXPORT_ATTEMPT_AT <= %s)",
            (at, user_id, not_before),
        )
        return cur.rowcount > 0

    @staticmethod
    def mark_key_exported(db: psycopg.Connection, user_id: str, at: int) -> bool:
        """First export only — a later one must not move the stamp, or the
        re-grant lock would appear to lapse."""
        cur = db.execute(
            "UPDATE users SET KEY_EXPORTED_AT = %s "
            "WHERE USER_ID = %s AND KEY_EXPORTED_AT IS NULL",
            (at, user_id),
        )
        return cur.rowcount > 0

    @staticmethod
    def link_google_identity(
        db: psycopg.Connection, user_id: str, google_sub: str
    ) -> bool:
        """Hand an existing account to a Google identity.

        The password goes with the stamp, in one statement. We never verified
        that whoever set that password owns the address -- registration has no
        email confirmation -- so anybody could have claimed a stranger's
        address before its owner ever arrived. The Google token is the only
        proof of ownership in play, so it takes the account whole.
        """
        cur = db.execute(
            "UPDATE users SET GOOGLE_SUB = %s, PASSWORD_HASH = NULL "
            "WHERE USER_ID = %s",
            (google_sub, user_id),
        )
        return cur.rowcount > 0

    @staticmethod
    def set_workos_user_id(
        db: psycopg.Connection, user_id: str, workos_user_id: str
    ) -> bool:
        """Link this account to its WorkOS identity. False when no row matched.

        Leaves PASSWORD_HASH alone: this is the writer the migration script
        uses to backfill ids for accounts nobody has signed into yet, and
        those accounts must keep logging in with their password until plan 3
        removes that door. `link_workos_identity` below is the one to use when
        somebody has just proved they own the address.
        """
        cur = db.execute(
            "UPDATE users SET WORKOS_USER_ID = %s WHERE USER_ID = %s",
            (workos_user_id, user_id),
        )
        return cur.rowcount > 0

    @staticmethod
    def link_workos_identity(
        db: psycopg.Connection, user_id: str, workos_user_id: str
    ) -> bool:
        """Hand an existing account to a WorkOS identity. False when no row matched.

        **The password is deliberately left alone**, and this is not the same
        call as `link_google_identity` above, which clears it.

        The argument for clearing is good and will be acted on: registration
        takes any address on trust, so a password sitting on a row is no
        evidence that whoever set it owns the address, while a mailed code is.
        Key export is no longer what stands in the way -- `export_private_key`
        stopped reading PASSWORD_HASH and now re-authenticates every account
        the same way, with a mailed code pinned to WORKOS_USER_ID.

        The rollback is. `/login` answers 410 since the cutover, but the
        service behind it was left untouched for exactly this reason:
        reverting that one commit restores a working legacy sign-in, and what
        it signs people in with is these hashes. `change_password` reads the
        column live either way. Nulling the hash here spends that credential
        account by account, silently, on each holder's first code sign-in --
        all 17 production accounts have one -- and it does not come back.

        The door is shut and the password outlives it by one plan: plan 4
        drops the column, and until then the row keeps its password, which is
        exactly the situation before this feature existed.
        """
        cur = db.execute(
            "UPDATE users SET WORKOS_USER_ID = %s WHERE USER_ID = %s",
            (workos_user_id, user_id),
        )
        return cur.rowcount > 0

    @staticmethod
    def set_last_topup_at(db: psycopg.Connection, user_id: str, at: int | None) -> None:
        """Unconditional write. Used to release a claim after a failed mint —
        `at` may be the prior (possibly None) value being restored."""
        db.execute(
            "UPDATE users SET LAST_TOPUP_AT = %s WHERE USER_ID = %s", (at, user_id)
        )

    @staticmethod
    def claim_topup(
        db: psycopg.Connection,
        user_id: str,
        at: int,
        not_before: int,
        deposit_raw: int,
        default_raw: int,
    ) -> bool:
        """Take the day's top-up allowance and record the deposit, atomically.

        Returns False when another request already holds it. The predicate, the
        cooldown stamp and the deposit are one statement so two concurrent
        callers cannot both pass a check-then-write gap, and so a claim can
        never be recorded without its deposit.

        `default_raw` seeds a NULL column the same way `get_total_deposited`
        reads one: NULL means "predates this column", and the writer and the
        reader must agree on what that means, or the first claim on a
        never-recorded account silently overwrites the signup grant with just
        that claim's deposit.
        """
        cur = db.execute(
            "UPDATE users SET LAST_TOPUP_AT = %s, "
            "TOTAL_DEPOSITED = COALESCE(TOTAL_DEPOSITED, %s) + %s "
            "WHERE USER_ID = %s "
            "AND (LAST_TOPUP_AT IS NULL OR LAST_TOPUP_AT <= %s)",
            (at, default_raw, deposit_raw, user_id, not_before),
        )
        return cur.rowcount == 1

    @staticmethod
    def release_topup(
        db: psycopg.Connection, user_id: str, last: int | None, deposit_raw: int
    ) -> None:
        """Undo a claim whose mint never landed — both halves of it.

        `COALESCE(..., 0)` here (not a `default_raw`) is safe: this only ever
        runs after `claim_topup` has already committed a claim for the same
        user, so TOTAL_DEPOSITED cannot still be NULL by the time this runs.
        """
        db.execute(
            "UPDATE users SET LAST_TOPUP_AT = %s, "
            "TOTAL_DEPOSITED = COALESCE(TOTAL_DEPOSITED, 0) - %s "
            "WHERE USER_ID = %s",
            (last, deposit_raw, user_id),
        )

    @staticmethod
    def set_total_deposited(
        db: psycopg.Connection, user_id: str, raw: int
    ) -> None:
        db.execute(
            "UPDATE users SET TOTAL_DEPOSITED = %s WHERE USER_ID = %s",
            (raw, user_id),
        )

    @staticmethod
    def set_deployment_id(
        db: psycopg.Connection, user_id: str, deployment_id: str
    ) -> None:
        db.execute(
            "UPDATE users SET DEPLOYMENT_ID = %s WHERE USER_ID = %s",
            (deployment_id, user_id),
        )

    @staticmethod
    def reset_deposits(
        db: psycopg.Connection, user_id: str, deployment_id: str
    ) -> bool:
        """Start the deposit ledger over against a new deployment.

        Conditional on the row still carrying an older identity, so this is a
        compare-and-swap rather than a blind SET. Two callers who both noticed
        the same wipe are safe: the first swaps the identity, and the second
        matches nothing and changes no row -- which matters because by then the
        first may already have claimed and minted, and an unconditional SET
        would erase that deposit with no way to repair it.

        Returns whether this call was the one that performed the reset.
        """
        cur = db.execute(
            "UPDATE users SET TOTAL_DEPOSITED = 0, DEPLOYMENT_ID = %s "
            "WHERE USER_ID = %s AND DEPLOYMENT_ID IS DISTINCT FROM %s",
            (deployment_id, user_id, deployment_id),
        )
        return cur.rowcount == 1

    @staticmethod
    def insert_account_snapshot(
        db: psycopg.Connection,
        user_id: str,
        t: int,
        capital_raw: int,
        deposited_raw: int,
        invested_raw: int = 0,
        unrealized_raw: int = 0,
    ) -> None:
        """One valuation of an account.

        `invested_raw` is the cost basis of the open positions -- what the
        account put to work, not what it was handed. `unrealized_raw` is what
        those positions have gained or lost on paper; the realized half is the
        residual against (capital - deposited).
        """
        db.execute(
            "INSERT INTO account_snapshots "
            "(USER_ID, T, CAPITAL_RAW, DEPOSITED_RAW, INVESTED_RAW, "
            "UNREALIZED_RAW) VALUES (%s, %s, %s, %s, %s, %s)",
            (user_id, t, capital_raw, deposited_raw, invested_raw, unrealized_raw),
        )

    @staticmethod
    def prune_account_snapshots(db: psycopg.Connection, older_than: int) -> int:
        cur = db.execute(
            "DELETE FROM account_snapshots WHERE T < %s", (older_than,)
        )
        return cur.rowcount

    @staticmethod
    def mark_user_as_bot(db: psycopg.Connection, api_key: str) -> bool:
        cur = db.execute("UPDATE users SET IS_BOT = 1 WHERE API_KEY = %s", (api_key,))
        return cur.rowcount > 0

    @staticmethod
    def mark_user_as_bot_by_eth_address(
        db: psycopg.Connection, eth_address: str
    ) -> bool:
        cur = db.execute(
            "UPDATE users SET IS_BOT = 1 WHERE LOWER(ETH_ADDRESS) = LOWER(%s)",
            (eth_address,),
        )
        return cur.rowcount > 0

    @staticmethod
    def create_personality(
        db: psycopg.Connection,
        personality_id: str,
        title: str,
        beliefs: str,
        methods: str,
        needs: str,
    ) -> str:
        spec = json.dumps(
            {"beliefs": beliefs, "methods": methods, "needs": needs},
            separators=(",", ":"),
        )
        db.execute(
            """
            INSERT INTO personalities (PERSONALITY_ID, PERSONALITY_TITLE, PERSONALITY_SPEC)
            VALUES (%s, %s, %s)
            """,
            (personality_id, title, spec),
        )
        return personality_id

    @staticmethod
    def create_agent(
        db: psycopg.Connection, agent_id: str, personality_id: str
    ) -> None:
        db.execute(
            """
            INSERT INTO agents (AGENT_ID, PERSONALITY)
            VALUES (%s, %s)
            """,
            (agent_id, personality_id),
        )

    @staticmethod
    def upsert_event(
        db: psycopg.Connection,
        *,
        slug: str,
        title: str,
        description: str = "",
        icon_url: str | None = None,
        category: str | None = None,
        start_date: int | None = None,
        end_date: int | None = None,
        polymarket_event_id: str | None = None,
    ) -> Event:
        """Insert an event or update it if SLUG already exists.

        Used by both the seeder and the Polymarket sync to be idempotent.

        ICON_URL, CATEGORY and POLYMARKET_EVENT_ID are COALESCE-updated: a
        caller that does not know the value (the singleton auto-wrap, which
        runs on every startup, and the sync's no-metadata branch) passes None
        and must not erase what an earlier, better-informed write stored. The
        returned Event reflects what is actually in the row afterwards.
        """
        existing = db.execute(
            "SELECT EVENT_ID FROM events WHERE SLUG = %s LIMIT 1", (slug,)
        ).fetchone()
        if existing is not None:
            event_id = int(existing["EVENT_ID"])
            updated = db.execute(
                """
                UPDATE events
                SET TITLE = %s,
                    DESCRIPTION = %s,
                    ICON_URL = COALESCE(%s, ICON_URL),
                    CATEGORY = COALESCE(%s, CATEGORY),
                    START_DATE = %s,
                    END_DATE = %s,
                    POLYMARKET_EVENT_ID = COALESCE(%s, POLYMARKET_EVENT_ID)
                WHERE EVENT_ID = %s
                RETURNING ICON_URL, CATEGORY, POLYMARKET_EVENT_ID
                """,
                (
                    title,
                    description,
                    icon_url,
                    category,
                    start_date,
                    end_date,
                    polymarket_event_id,
                    event_id,
                ),
            ).fetchone()
            if updated is not None:
                icon_url = updated["ICON_URL"]
                category = updated["CATEGORY"]
                polymarket_event_id = updated["POLYMARKET_EVENT_ID"]
        else:
            row = db.execute(
                """
                INSERT INTO events (SLUG, TITLE, DESCRIPTION, ICON_URL, CATEGORY,
                                    START_DATE, END_DATE, POLYMARKET_EVENT_ID)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING EVENT_ID
                """,
                (
                    slug,
                    title,
                    description,
                    icon_url,
                    category,
                    start_date,
                    end_date,
                    polymarket_event_id,
                ),
            ).fetchone()
            event_id = int(row["EVENT_ID"])
        return Event(
            event_id=event_id,
            slug=slug,
            title=title,
            description=description,
            icon_url=icon_url,
            category=category,
            start_date=start_date,
            end_date=end_date,
            polymarket_event_id=polymarket_event_id,
        )

    @staticmethod
    def update_event_volume(
        db: psycopg.Connection,
        event_id: int,
        volume_24hr: float | None,
        volume: float | None = None,
    ) -> None:
        """Refresh an event's captured upstream volumes.

        Each figure is skipped independently when None, so a degraded sync pass
        (e.g. a recurring window with no series metadata, whose own 24h volume is
        null) never clobbers a previously-captured good value -- and a payload
        carrying only one of the two updates only that one. Called on every bind
        pass so the figures track the latest sync.
        """
        sets = []
        params: list[object] = []
        if volume_24hr is not None:
            sets.append("VOLUME_24HR = %s")
            params.append(volume_24hr)
        if volume is not None:
            sets.append("VOLUME = %s")
            params.append(volume)
        if not sets:
            return
        params.append(event_id)
        db.execute(
            f"UPDATE events SET {', '.join(sets)} WHERE EVENT_ID = %s",
            tuple(params),
        )

    @staticmethod
    def update_event_metrics(
        db: psycopg.Connection,
        event_id: int,
        liquidity: float | None,
        competitive: float | None,
    ) -> None:
        """Refresh an event's captured order-book depth and contest score.

        Each figure is skipped independently when None, exactly as
        `update_event_volume` treats the volumes: a payload carrying only one
        of the two must not blank the other, and a pass where upstream sent
        neither must not blank both. Called on every bind pass.
        """
        sets = []
        params: list[object] = []
        if liquidity is not None:
            sets.append("LIQUIDITY = %s")
            params.append(liquidity)
        if competitive is not None:
            sets.append("COMPETITIVE = %s")
            params.append(competitive)
        if not sets:
            return
        params.append(event_id)
        db.execute(
            f"UPDATE events SET {', '.join(sets)} WHERE EVENT_ID = %s",
            tuple(params),
        )

    @staticmethod
    def update_market_polymarket_tokens(
        db: psycopg.Connection,
        *,
        polymarket_id: int,
        yes_token_id: str | None,
        no_token_id: str | None,
    ) -> None:
        """Backfill the upstream Polymarket token-id cross-reference on an
        already-synced market (matched by polymarket_id).

        COALESCE so a None never clobbers an existing id; no-op when both are
        None. Lets the book mirror resolve markets synced before positional
        token capture existed (e.g. Up/Down windows, whose yes/no ids were null).
        """
        if yes_token_id is None and no_token_id is None:
            return
        db.execute(
            """
            UPDATE markets
            SET POLYMARKET_YES_TOKEN_ID = COALESCE(%s, POLYMARKET_YES_TOKEN_ID),
                POLYMARKET_NO_TOKEN_ID  = COALESCE(%s, POLYMARKET_NO_TOKEN_ID)
            WHERE POLYMARKET_ID = %s
            """,
            (yes_token_id, no_token_id, polymarket_id),
        )

    @staticmethod
    def attach_market_to_event(
        db: psycopg.Connection,
        *,
        market_id: int,
        event_id: int,
        outcome_label: str | None = None,
        icon_url: str | None = None,
    ) -> None:
        """Bind an existing market to an event, optionally setting display metadata."""
        db.execute(
            """
            UPDATE markets
            SET EVENT_ID = %s,
                OUTCOME_LABEL = COALESCE(%s, OUTCOME_LABEL),
                ICON_URL      = COALESCE(%s, ICON_URL)
            WHERE MARKET_ID = %s
            """,
            (event_id, outcome_label, icon_url, market_id),
        )

    @staticmethod
    def replace_market_tags(
        db: psycopg.Connection, *, market_id: int, tags: list[tuple[str, str]]
    ) -> None:
        """Set this market's tag rows to exactly ``tags``, a list of
        ``(slug, label)`` pairs the caller has already normalised.

        Replace rather than merge: a tag Polymarket removed must disappear
        locally on the next sync pass, and only a full rewrite of one market's
        set achieves that without needing to know the previous contents.

        Duplicate slugs within one call are collapsed. Two upstream entries can
        normalise to the same slug (Gamma has returned both "1H" and "1h"), and
        a duplicate row would violate the primary key and abort the statement,
        taking the caller's whole transaction with it.
        """
        db.execute("DELETE FROM market_tags WHERE MARKET_ID = %s", (market_id,))
        deduped: dict[str, str] = {}
        for slug, label in tags:
            deduped.setdefault(slug, label)
        if not deduped:
            return
        with db.cursor() as cur:
            cur.executemany(
                "INSERT INTO market_tags (MARKET_ID, SLUG, LABEL) VALUES (%s, %s, %s)",
                [(market_id, slug, label) for slug, label in deduped.items()],
            )

    @staticmethod
    def update_event_category(
        db: psycopg.Connection, *, event_id: int, category: str
    ) -> None:
        """Set an event's category, leaving every other column alone.

        Deliberately not routed through ``upsert_event``: that matches on SLUG,
        but the sync finds already-known events by POLYMARKET_EVENT_ID. If
        upstream renamed the slug, a full upsert would insert a duplicate row
        instead of updating the existing one.
        """
        db.execute(
            "UPDATE events SET CATEGORY = %s WHERE EVENT_ID = %s",
            (category, event_id),
        )

    @staticmethod
    def create_market(
        db: psycopg.Connection, request: CreateMarketRequest, is_polygon_market: bool
    ) -> Market:

        # The local create-market path now sets `request.condition_id` upstream
        # (in MarketService) using the on-chain `getConditionId` view, so by the
        # time we get here the condition_id is always present.
        check_state(
            request.condition_id is not None,
            "request.condition_id must be set before TableWrite.create_market",
        )
        condition_id = request.condition_id

        erc1155_tokens_json = json.dumps(request.erc1155_tokens, separators=(",", ":"))

        # MARKET_ID is a BIGINT IDENTITY column — let Postgres assign it (avoids
        # the old racy MAX(MARKET_ID)+1) and read it back with RETURNING.
        new_market_id = int(
            db.execute(
                """
                INSERT INTO markets (CONDITION_ID,
                                     POLYMARKET_ID,
                                     POLYMARKET_CONDITION_ID,
                                     QUESTION,
                                     DESCRIPTION,
                                     SLUG,
                                     START_DATE,
                                     END_DATE,
                                     ERC1155_TOKENS,
                                     MARKET_STATE,
                                     EVENT_ID,
                                     OUTCOME_LABEL,
                                     ICON_URL,
                                     POLYMARKET_YES_TOKEN_ID,
                                     POLYMARKET_NO_TOKEN_ID)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING MARKET_ID
                """,
                (
                    condition_id.value,
                    request.polymarket_id,
                    request.polymarket_condition_id,
                    request.question,
                    request.description,
                    request.slug,
                    request.start_date,
                    request.end_date,
                    erc1155_tokens_json,
                    request.state,
                    request.event_id,
                    request.outcome_label,
                    request.icon_url,
                    request.polymarket_yes_token_id,
                    request.polymarket_no_token_id,
                ),
            ).fetchone()["MARKET_ID"]
        )

        return Market(
            question=request.question,
            market_id=new_market_id,
            polymarket_id=request.polymarket_id,
            polymarket_condition_id=request.polymarket_condition_id,
            polymarket_yes_token_id=request.polymarket_yes_token_id,
            polymarket_no_token_id=request.polymarket_no_token_id,
            condition_id=condition_id,
            description=request.description,
            slug=request.slug,
            erc1155_tokens=request.erc1155_tokens,
            market_state=MarketState(request.state),
            start_date=request.start_date,
            end_date=request.end_date,
            resolved_outcome=None,
            event_id=request.event_id,
            outcome_label=request.outcome_label,
            icon_url=request.icon_url,
        )

    @staticmethod
    def update_market_state_if_needed(
        db: psycopg.Connection, request: CreateMarketRequest
    ) -> Market:
        # Compute condition_id from question and number of outcomes
        erc1155_tokens_json = json.dumps(request.erc1155_tokens, separators=(",", ":"))

        # Fetch existing market details to preserve state and IDs
        cursor = db.execute(
            "SELECT MARKET_ID, RESOLVED_OUTCOME FROM markets WHERE POLYMARKET_ID = %s",
            (request.polymarket_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise ValueError(
                f"Market with Polymarket ID {request.polymarket_id} not found"
            )

        market_id = row["MARKET_ID"]
        resolved_outcome = row["RESOLVED_OUTCOME"]

        db.execute(
            """
            UPDATE markets
            SET CONDITION_ID   = %s,
                QUESTION       = %s,
                DESCRIPTION    = %s,
                SLUG           = %s,
                START_DATE     = %s,
                END_DATE       = %s,
                ERC1155_TOKENS = %s,
                MARKET_STATE  = %s
            WHERE POLYMARKET_ID = %s
            """,
            (
                request.condition_id,
                request.question,
                request.description,
                request.slug,
                request.start_date,
                request.end_date,
                erc1155_tokens_json,
                request.state,
                request.polymarket_id,
            ),
        )

        return Market(
            question=request.question,
            market_id=market_id,
            polymarket_id=request.polymarket_id,
            condition_id=request.condition_id,
            description=request.description,
            slug=request.slug,
            erc1155_tokens=request.erc1155_tokens,
            market_state=MarketState(request.state),
            start_date=request.start_date,
            end_date=request.end_date,
            resolved_outcome=resolved_outcome,
        )

    @staticmethod
    def log_transaction(
        db: psycopg.Connection,
        api_key: str,
        transaction_type: str,
        market_id: int | None = None,
        details: dict | None = None,
    ) -> None:
        """
        Log a transaction to the transactions table.

        Args:
            db: Database connection
            api_key: API key of the user performing the transaction
            transaction_type: Type of transaction (e.g., "SPLIT", "MERGE", "REDEEM")
            market_id: Optional market ID
            details: Optional dictionary with transaction details (will be JSON serialized)
        """
        details_json = json.dumps(details) if details else None

        db.execute(
            """
            INSERT INTO transactions (API_KEY, TRANSACTION_TYPE, MARKET_ID, DETAILS)
            VALUES (%s, %s, %s, %s)
            """,
            (api_key, transaction_type, market_id, details_json),
        )

    @staticmethod
    def activate_market(db: psycopg.Connection, market_id: int) -> Market:
        """
        Activate a market, transitioning it from DRAFT to ACTIVE.

        Args:
            db: Database connection
            market_id: Market ID to activate

        Returns:
            Updated Market object

        Raises:
            ValueError: If market not found or not in DRAFT state
        """
        from agentpit.db.table_read import TableRead

        # Get current market
        market = TableRead.read_market(db, market_id)
        if not market:
            raise ValueError(f"Market {market_id} not found")

        # Check state
        if market.market_state != MarketState.DRAFT:
            raise ValueError(
                f"Market {market_id} is not in DRAFT state (current: {market.market_state.value})"
            )

        # Update state
        db.execute(
            "UPDATE markets SET MARKET_STATE = %s WHERE MARKET_ID = %s",
            (MarketState.ACTIVE.value, market_id),
        )

        # Return updated market
        market.market_state = MarketState.ACTIVE
        return market

    @staticmethod
    def close_market(db: psycopg.Connection, market_id: int) -> Market:
        """
        Close a market, transitioning it from ACTIVE to CLOSED.

        Args:
            db: Database connection
            market_id: Market ID to close

        Returns:
            Updated Market object

        Raises:
            ValueError: If market not found or not in ACTIVE state
        """
        from agentpit.db.table_read import TableRead

        # Get current market
        market = TableRead.read_market(db, market_id)
        if not market:
            raise ValueError(f"Market {market_id} not found")

        # Check state
        if market.market_state != MarketState.ACTIVE:
            raise ValueError(
                f"Market {market_id} is not in ACTIVE state (current: {market.market_state.value})"
            )

        # Update state to CLOSED
        db.execute(
            "UPDATE markets SET MARKET_STATE = %s WHERE MARKET_ID = %s",
            (MarketState.CLOSED.value, market_id),
        )

        # Return updated market
        market.market_state = MarketState.CLOSED
        return market

    @staticmethod
    def mark_fully_redeemed(db: psycopg.Connection, market_id: int) -> None:
        """Flag a resolved market as having no remaining redeemable holders."""
        db.execute(
            "UPDATE markets SET FULLY_REDEEMED = TRUE WHERE MARKET_ID = %s",
            (market_id,),
        )

    @staticmethod
    def resolve_market(
        db: psycopg.Connection, market_id: int, winning_outcome_index: int
    ) -> Market:
        """
        Resolve a market by specifying the winning outcome.

        Args:
            db: Database connection
            market_id: Market ID to resolve
            winning_outcome_index: Index of the winning outcome (0-based)

        Returns:
            Updated Market object

        Raises:
            ValueError: If market not found, already resolved, or invalid outcome index
        """
        from agentpit.db.table_read import TableRead

        # Get current market
        market = TableRead.read_market(db, market_id)
        if not market:
            raise ValueError(f"Market {market_id} not found")

        # Check if already resolved
        if market.market_state == MarketState.RESOLVED:
            raise ValueError(f"Market {market_id} is already resolved")

        # Validate outcome index
        if winning_outcome_index < 0 or winning_outcome_index >= len(
            market.erc1155_tokens
        ):
            raise ValueError(
                f"Invalid winning_outcome_index {winning_outcome_index}. "
                f"Market has {len(market.erc1155_tokens)} outcomes (indices 0-{len(market.erc1155_tokens)-1})"
            )

        # Update state and outcome
        db.execute(
            "UPDATE markets SET MARKET_STATE = %s, RESOLVED_OUTCOME = %s WHERE MARKET_ID = %s",
            (MarketState.RESOLVED.value, winning_outcome_index, market_id),
        )

        # Return updated market
        market.market_state = MarketState.RESOLVED
        market.resolved_outcome = winning_outcome_index
        return market

    @staticmethod
    def cancel_market(db: psycopg.Connection, market_id: int) -> tuple[Market, int]:
        """Cancel a market.

        Refund logic is intentionally not implemented here: with on-chain CTF
        positions, users recover their collateral via the standard merge /
        redeem path on the CTF contract directly, not via the backend.
        """
        from agentpit.db.table_read import TableRead

        market = TableRead.read_market(db, market_id)
        if not market:
            raise ValueError(f"Market {market_id} not found")

        if market.market_state == MarketState.RESOLVED:
            raise ValueError(f"Cannot cancel market {market_id}: already resolved")
        if market.market_state == MarketState.CANCELLED:
            raise ValueError(f"Market {market_id} is already cancelled")

        db.execute(
            "UPDATE markets SET MARKET_STATE = %s WHERE MARKET_ID = %s",
            (MarketState.CANCELLED.value, market_id),
        )

        market.market_state = MarketState.CANCELLED
        return market, 0

    @staticmethod
    def update_market_state_to_resolved_if_needed(
        db: psycopg.Connection, condition_id: ConditionId, winning_outcome_index: int
    ) -> Market:
        """
        Idempotently resolves a market. If already resolved or cancelled, does nothing.

        Args:
            db: Database connection
            condition_id: Condition ID of the market to resolve
            winning_outcome_index: Index of the winning outcome

        Returns:
            Updated Market object
        """
        from agentpit.db.table_read import TableRead

        # Get current market
        market = TableRead.read_market_by_condition_id(db, condition_id)
        if not market:
            raise ValueError(f"Market with condition_id {condition_id} not found")

        if (
            market.market_state == MarketState.RESOLVED
            or market.market_state == MarketState.CANCELLED
        ):
            return market

        # Validate outcome index
        if winning_outcome_index < 0 or winning_outcome_index >= len(
            market.erc1155_tokens
        ):
            raise ValueError(
                f"Invalid winning_outcome_index {winning_outcome_index}. "
                f"Market has {len(market.erc1155_tokens)} outcomes"
            )

        db.execute(
            "UPDATE markets SET MARKET_STATE = %s, RESOLVED_OUTCOME = %s WHERE CONDITION_ID = %s",
            (
                MarketState.RESOLVED.value,
                winning_outcome_index,
                market.condition_id.value,
            ),
        )

        market.market_state = MarketState.RESOLVED
        market.resolved_outcome = winning_outcome_index
        return market

    @staticmethod
    def expire_due_orders(db: psycopg.Connection, now: int) -> int:
        """Mark GTD orders past their grace as cancelled. Returns rows marked.

        Hygiene, not correctness: `TableRead.LIVE_ORDER` already excludes
        every row this touches, so running late or not at all trades nothing.
        What it buys is that dead rows leave the live set for good and become
        reachable by `purge_cancelled_orders`.

        No new status, because Polymarket has none — their order statuses are
        live, matched, delayed and unmatched, and expiry is a property rather
        than a state. The row keeps its EXPIRATION, so why it went is still
        legible.

        `STATUS = %s` rather than a literal 'live': `test_live_order_guard`
        bans spelling that comparison out anywhere but `TableRead.LIVE_ORDER`
        itself, on the theory that a hand-rolled copy is the one that drifts
        and quietly trades an expired order. This query doesn't reuse
        `LIVE_ORDER` — it targets the complement of its expiration arm, not
        the same set — so a bound parameter keeps the literal out of the SQL
        text without re-deriving that predicate by hand.
        """
        cur = db.execute(
            "UPDATE orders SET STATUS = 'cancelled' "
            "WHERE STATUS = %s AND EXPIRATION > 0 AND EXPIRATION <= %s + %s",
            ("live", now, TableRead.EXPIRY_GRACE_SECONDS),
        )
        return cur.rowcount

    @staticmethod
    def purge_cancelled_orders(db: psycopg.Connection, before_ts: int) -> int:
        """Delete cancelled orders created before `before_ts`. Cancelled orders
        are resting quotes replaced before matching, so nothing references them;
        this caps table growth from fast re-quoting. Returns rows removed.

        Idempotency claims pointing at a purged order go with it — a claim
        whose order row is gone would break every replay of that
        client_order_id until the (much longer) key retention expires it."""
        db.execute(
            "DELETE FROM idempotency_keys WHERE ORDER_ID IN "
            "(SELECT ORDER_ID FROM orders "
            " WHERE STATUS = 'cancelled' AND CREATED_AT < %s)",
            (before_ts,),
        )
        cur = db.execute(
            "DELETE FROM orders WHERE STATUS = 'cancelled' AND CREATED_AT < %s",
            (before_ts,),
        )
        return cur.rowcount

    @staticmethod
    def delete_idempotency_key(
        db: psycopg.Connection, *, api_key: str, client_order_id: str
    ) -> None:
        """Drop a single (api_key, client_order_id) claim — used to heal a
        stale claim whose order row no longer exists."""
        db.execute(
            "DELETE FROM idempotency_keys "
            "WHERE API_KEY = %s AND CLIENT_ORDER_ID = %s",
            (api_key, client_order_id),
        )

    @staticmethod
    def claim_idempotency_key(
        db: psycopg.Connection,
        *,
        api_key: str,
        client_order_id: str,
        order_id: str,
        created_at: int,
    ) -> None:
        """Reserve (api_key, client_order_id) for this order. Raises
        psycopg.errors.UniqueViolation if already claimed — the caller treats
        that as a duplicate and replays the prior order."""
        db.execute(
            "INSERT INTO idempotency_keys "
            "(API_KEY, CLIENT_ORDER_ID, ORDER_ID, CREATED_AT) "
            "VALUES (%s, %s, %s, %s)",
            (api_key, client_order_id, order_id, created_at),
        )

    @staticmethod
    def purge_idempotency_keys(db: psycopg.Connection, before_ts: int) -> int:
        """Delete idempotency keys created before `before_ts`. Returns rows removed."""
        cur = db.execute(
            "DELETE FROM idempotency_keys WHERE CREATED_AT < %s",
            (before_ts,),
        )
        return cur.rowcount
