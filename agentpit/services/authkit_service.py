"""Turning a WorkOS identity into an agentpit account.

WorkOS proves that somebody owns an email address. Everything that makes the
account an agentpit account -- the wallet, its private key, the API key, the
handle, the on-chain onboarding -- is made here, on the first successful
sign-in and never again.

There is no registration endpoint and no registration step. A person who has
never been here and a person who signs in daily take the same path; the only
difference is whether `WORKOS_USER_ID` already matches a row.
"""
import logging
import time
from dataclasses import dataclass

from psycopg.errors import UniqueViolation

from agentpit.datastructures.user import User
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import (
    AuthCodeRateLimitedError,
    InvalidCredentialsError,
)
from agentpit.domain.handles import pick_handle
from agentpit.auth.workos_client import WorkOsClient, WorkOsSession

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthKitSession:
    user: User
    access_token: str
    refresh_token: str


class AuthKitService:
    def __init__(
        self,
        *,
        db: DbSession,
        workos: WorkOsClient,
        onboard,
        reonboard,
        per_email_hourly: int = 20,
        per_ip_hourly: int = 60,
    ):
        self._db = db
        self._workos = workos
        # `AuthService._onboard_new_account` and `AuthService._maybe_reonboard`
        # -- injected rather than imported so the chain stays out of these
        # tests, and so the two services do not depend on each other's
        # construction.
        self._onboard = onboard
        self._reonboard = reonboard
        self._per_email_hourly = per_email_hourly
        self._per_ip_hourly = per_ip_hourly

    #: One hour, fixed window. Two subjects, because either alone leaves the
    #: other's hole open: keyed on the ADDRESS an attacker rotates addresses and
    #: still spends our money, keyed on the IP alone one inbox can be filled
    #: from many hosts.
    #:
    #: There is deliberately NO per-minute rule. One existed and was removed:
    #: the dialog's own cooldown guards only its resend button, not a fresh
    #: submit, so a person who closed the dialog and tried the same address
    #: again -- exactly what somebody does when the mail is slow -- was told
    #: "too many attempts" for behaving normally.
    RATE_LIMIT_WINDOW_SECONDS = 3600

    def send_code(self, email: str, *, client_ip: str | None = None) -> None:
        """Mail a code, unless this address or this caller has had enough.

        Every rule is checked BEFORE the WorkOS call, and that ordering is the
        whole point: a refused request must cost one row write, not an email
        and an HTTP round trip to another provider.

        `client_ip` is None when the request arrived without one the app is
        willing to trust. That skips the per-IP rule rather than lumping every
        caller into a single bucket -- a shared bucket would turn the per-IP
        rule into a global kill switch, which is a denial of service somebody
        could trigger on purpose.
        """
        rules = (
            ("email", email.strip().lower(), self._per_email_hourly),
            ("ip", client_ip, self._per_ip_hourly),
        )
        with self._db.write() as conn:
            for kind, subject, limit in rules:
                if subject is None:
                    continue
                allowed = TableWrite.claim_auth_code_attempt(
                    conn,
                    f"{kind}:1h:{subject}",
                    int(time.time()),
                    self.RATE_LIMIT_WINDOW_SECONDS,
                    limit,
                )
                if not allowed:
                    # The window length is the honest `Retry-After`: a fixed
                    # window can free up sooner, never later.
                    raise AuthCodeRateLimitedError(
                        retry_after=self.RATE_LIMIT_WINDOW_SECONDS
                    )

        self._workos.send_magic_auth_code(email)

    def sign_in(self, email: str, code: str) -> AuthKitSession:
        session = self._workos.authenticate_with_code(email, code)
        return AuthKitSession(
            user=self._resolve_account(session, create=True, repair=True),
            access_token=session.access_token,
            refresh_token=session.refresh_token,
        )

    def sign_in_with_authorization_code(self, code: str) -> AuthKitSession:
        """A provider redirect (today: Google) landing back on our callback.

        Deliberately identical to `sign_in` past the exchange: whichever door
        an identity arrives through, the account is resolved and created the
        same way. `repair=True` for the same reason as `sign_in` -- a person is
        at the screen, having just come back from Google.
        """
        session = self._workos.authenticate_with_authorization_code(code)
        return AuthKitSession(
            user=self._resolve_account(session, create=True, repair=True),
            access_token=session.access_token,
            refresh_token=session.refresh_token,
        )

    def refresh(self, refresh_token: str) -> AuthKitSession:
        session = self._workos.refresh_session(refresh_token)
        return AuthKitSession(
            # `create=False`: a refresh proves a session is alive, not that a
            # person just proved ownership of an address. Minting an account
            # here would put a wallet behind a credential that never passed
            # through sign_in.
            #
            # `repair=False` for a related reason and a practical one. A
            # refresh runs about every 300 seconds with nobody watching, and
            # both branches of `_repair` are chain work: onboarding drips
            # collateral, tops the wallet up and sends three approvals, and
            # `_reonboard` reads the wallet's nonce off the chain first. Every
            # request the page has in flight is queued behind the shared
            # refresh while that runs, and an onboarding that failed once will
            # most likely fail again.
            # The repair is not lost -- the next sign_in performs it, with a
            # person at the screen who is already paying that second.
            user=self._resolve_account(session, create=False, repair=False),
            access_token=session.access_token,
            refresh_token=session.refresh_token,
        )

    def _resolve_account(
        self, session: WorkOsSession, *, create: bool, repair: bool
    ) -> User:
        with self._db.read() as conn:
            user = TableRead.get_user_by_workos_id(conn, session.workos_user_id)
        if user is not None:
            return self._repair(user) if repair else user
        if not create:
            raise InvalidCredentialsError("invalid session")
        return self._create_account(session)

    def _repair(self, user: User) -> User:
        """The two ways an existing row can need chain work, and neither overlaps.

        `ONBOARDED_AT` null means the wallet was never funded at all --
        `_create_account` commits the row before onboarding it, so a chain that
        was down during a first sign-in leaves exactly this, and every later
        sign-in would otherwise hand back a session for a wallet that fails
        every order.

        `ONBOARDED_AT` set means the wallet WAS funded, and
        `AuthService._maybe_reonboard` is the repair for the other failure: the
        local anvil wipes its state on restart while the database persists, so
        a funded wallet can find itself empty. That method returns early
        precisely when `onboarded_at is None`, because it needs the stamp to
        know the wallet was ever funded -- which is why these are two branches
        and not one call.
        """
        if user.onboarded_at is None:
            log.info(
                "user %s has no ONBOARDED_AT — finishing onboarding on sign-in",
                user.user_id,
            )
            return self._onboard(user.user_id, user.eth_key)
        self._reonboard(user)
        return user

    def _link_existing_account(self, session: WorkOsSession) -> User | None:
        """Adopt an account that predates this address's WORKOS_USER_ID.

        None when there is no such row. Otherwise the row, with the identity
        stamped on it -- an account the migration missed, or one made by
        `/register` before this shipped. Linking beats creating: a second row
        is a second wallet and a person whose positions have disappeared.
        Case-insensitive for the same reason `get_user_by_email_ci` exists:
        WorkOS reports a normalised address, ours was stored as typed.
        """
        with self._db.write() as conn:
            existing = TableRead.get_user_by_email_ci(conn, session.email)
            if existing is None:
                return None
            # The password on that row goes with the link -- see
            # `TableWrite.link_workos_identity` for why. Nobody ever verified
            # that whoever set it owns this address; the code we just mailed is
            # the only proof of ownership in play, so it takes the account
            # whole.
            if not TableWrite.link_workos_identity(
                conn, existing.user_id, session.workos_user_id
            ):
                # The row went away between the read above and this write.
                # Issuing a session for it would hand back credentials for an
                # account that no longer exists.
                log.warning("workos link found no row for user %s", existing.user_id)
                raise InvalidCredentialsError("invalid session")
        # `existing` was read before the stamp, so reflect it locally rather
        # than re-reading the row. Only the identity changed: the password is
        # deliberately left in place (see `TableWrite.link_workos_identity`),
        # and `has_password` must keep saying so. Nothing about the wallet key
        # consults this flag -- keys cannot be exported at all -- but it still
        # describes the row, and the row still holds a hash that
        # `change_password` reads and that `/login` would accept again the
        # moment the cutover commit is reverted. A false here would be the
        # response denying a credential the row is still carrying.
        return existing.model_copy(
            update={"workos_user_id": session.workos_user_id}
        )

    def _create_account(self, session: WorkOsSession) -> User:
        linked = self._link_existing_account(session)
        if linked is not None:
            # Outside its transaction, like the create path below: a linked row
            # can itself be one whose onboarding never finished. Unconditional
            # because only `create=True` reaches here, and that means somebody
            # is at the screen having just proved they own the address --
            # exactly when `_resolve_account` repairs too.
            return self._repair(linked)

        try:
            with self._db.write() as conn:
                handle = pick_handle(
                    taken=lambda candidate: TableRead.handle_taken(conn, candidate)
                )
                user_id, acct, _api_key = TableWrite.create_user(
                    conn, email=session.email, password_hash=None, handle=handle
                )
                TableWrite.set_workos_user_id(
                    conn, user_id, session.workos_user_id
                )
        except UniqueViolation:
            # Another first sign-in for this same address committed between
            # `_link_existing_account`'s lookup and this insert. `EMAIL` and
            # `WORKOS_USER_ID` are both unique, so either can be the one that
            # fired; read back by identity first and fall back to the address.
            #
            # The loser gets the winner's account, which is the right answer:
            # one person, one wallet. The row may still have ONBOARDED_AT null
            # because the winner onboards outside its transaction -- that is
            # the condition `_repair` exists for, and the next sign-in closes
            # it.
            with self._db.read() as conn:
                winner = TableRead.get_user_by_workos_id(
                    conn, session.workos_user_id
                ) or TableRead.get_user_by_email_ci(conn, session.email)
            if winner is None:
                # The violation was something else entirely -- a handle
                # collision, say. Re-raising keeps a real bug visible instead
                # of turning it into a confusing None.
                raise
            # Logged only now. The read-back above is what establishes it was a
            # race at all: announcing one before it is confirmed puts a calm
            # INFO line immediately in front of the traceback from the `raise`
            # above, describing something that did not happen.
            log.info(
                "lost a create race for workos user %s — adopting the winner's row",
                session.workos_user_id,
            )
            return winner

        # Outside the transaction: onboarding is ~a second of chain round-trips
        # and must not hold a write lock, exactly as AuthService.register does it.
        return self._onboard(user_id, acct)
