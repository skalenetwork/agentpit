import logging
import time

from eth_account.signers.local import LocalAccount

from agentpit.auth.google import GoogleTokenVerifier
from agentpit.auth.jwt import JwtCoder
from agentpit.auth.passwords import hash_password, verify_password
from agentpit.config import Settings
from agentpit.datastructures.auth_response import (
    AuthResponse,
    GoogleAuthResponse,
    UserPublic,
)
from agentpit.datastructures.login_request import LoginRequest
from agentpit.datastructures.register_request import RegisterRequest
from agentpit.datastructures.user import User
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import (
    AdminGasPausedError,
    BusinessRuleError,
    FeatureDisabledError,
    InvalidCredentialsError,
    OnboardingError,
    UserAlreadyExistsError,
    UserNotFoundError,
)
from agentpit.domain.handles import pick_handle
from agentpit.onchain.admin import OnchainAdmin
from agentpit.onchain.deployment import is_disposable_chain

log = logging.getLogger(__name__)


class AuthService:
    """Coordinates registration, login, and on-chain onboarding."""

    #: A claim this old belongs to a process that died mid-onboarding. Five
    #: sequential chain sends at a 30 s timeout each fit inside it.
    ONBOARDING_CLAIM_STALE_S = 300

    def __init__(
        self,
        db: DbSession,
        coder: JwtCoder,
        onchain_admin: OnchainAdmin,
        settings: Settings,
        google_verifier: GoogleTokenVerifier | None = None,
    ):
        self._db = db
        self._coder = coder
        self._onchain = onchain_admin
        self._settings = settings
        self._google = google_verifier

    def register(self, payload: RegisterRequest) -> AuthResponse:
        with self._db.write() as conn:
            if TableRead.get_user_by_email_ci(conn, payload.email) is not None:
                raise UserAlreadyExistsError(payload.email)
            password_hash = hash_password(payload.password)
            # A supplied handle is a choice and is kept; a blank one is
            # filled. The availability check runs inside this transaction and
            # `HANDLE TEXT UNIQUE` is still the guarantee behind it -- two
            # signups landing on the same generated name in the same
            # millisecond would fail the insert rather than duplicate it,
            # which needs both a sub-millisecond overlap and the same 1-in-
            # 14,400 draw.
            handle = payload.handle or pick_handle(
                taken=lambda candidate: TableRead.handle_taken(conn, candidate)
            )
            user_id, acct, _api_key = TableWrite.create_user(
                conn,
                email=payload.email,
                password_hash=password_hash,
                handle=handle,
            )
        return self._issue(self._onboard_new_account(user_id, acct))

    def google_sign_in(self, credential: str) -> GoogleAuthResponse:
        """Sign in — or sign up — with a Google ID token.

        Lookup order is `sub` first, then the verified email. `sub` is Google's
        stable identifier; the address on a Google account can change, and an
        email-only lookup would then treat a returning user as a stranger and
        mint them a second wallet.
        """
        if self._google is None:
            raise FeatureDisabledError("Google sign-in is not configured")
        identity = self._google.verify(credential)

        with self._db.read() as conn:
            user = TableRead.get_user_by_google_sub(conn, identity.sub)
            by_email = (
                TableRead.get_user_by_email_ci(conn, identity.email)
                if user is None
                else None
            )

        if user is not None:
            self._maybe_reonboard(user)
            return self._google_response(user, created=False)

        if by_email is not None:
            # The same person arriving by a new door. Splitting them across two
            # accounts is not cosmetic: each one holds its own paper balance,
            # its own positions and its own standing on the board, so the
            # second would put their money somewhere they cannot see from where
            # they are standing.
            #
            # The password on that row goes with the link. Google verified this
            # address; we never did -- registration takes any address on trust
            # -- so a password already sitting on it is not evidence that
            # whoever set it owns the address. Leaving it would let somebody
            # who registered a stranger's address keep a working credential on
            # the account its real owner just walked into.
            with self._db.write() as conn:
                linked = TableWrite.link_google_identity(
                    conn, by_email.user_id, identity.sub
                )
            if not linked:
                # The row went away between the read above and this write.
                # Issuing a token for it would hand back credentials for an
                # account that no longer exists.
                log.warning(
                    "google link found no row for user %s", by_email.user_id
                )
                raise InvalidCredentialsError("invalid Google credential")
            # The link just cleared PASSWORD_HASH (see link_google_identity).
            # `by_email` was read before that write, so its `has_password`
            # would otherwise still say True -- reflect the change locally
            # rather than re-reading the row, so the response this call
            # issues doesn't claim a password that no longer exists.
            by_email = by_email.model_copy(update={"has_password": False})
            self._maybe_reonboard(by_email)
            return self._google_response(by_email, created=False)

        with self._db.write() as conn:
            handle = pick_handle(
                taken=lambda candidate: TableRead.handle_taken(conn, candidate)
            )
            user_id, acct, _api_key = TableWrite.create_user(
                conn,
                email=identity.email,
                password_hash=None,
                handle=handle,
                google_sub=identity.sub,
            )
        return self._google_response(
            self._onboard_new_account(user_id, acct), created=True
        )

    def login(self, payload: LoginRequest) -> AuthResponse:
        with self._db.read() as conn:
            user = TableRead.get_user_by_email(conn, payload.email)
            password_hash = (
                TableRead.get_password_hash_by_userid(conn, user.user_id)
                if user is not None
                else None
            )
        if user is None:
            raise InvalidCredentialsError("invalid email or password")
        if password_hash is None:
            # This account arrived through Google and has no password. Saying
            # "invalid email or password" would send somebody who has forgotten
            # which door they used around in a circle. It tells an attacker the
            # address is registered, which registration's 409 already does.
            raise InvalidCredentialsError("this account signs in with Google")
        if not verify_password(payload.password, password_hash):
            raise InvalidCredentialsError("invalid email or password")
        self._maybe_reonboard(user)
        return self._issue(user)

    def change_password(
        self,
        *,
        user_id: str,
        current_password: str,
        new_password: str,
    ) -> None:
        with self._db.write() as conn:
            if TableRead.get_user_by_userid(conn, user_id) is None:
                raise UserNotFoundError()
            current_hash = TableRead.get_password_hash_by_userid(conn, user_id)
            if current_hash is None:
                # A Google account has no password to change, and 404 "User not
                # found" would be a lie told to somebody who is signed in.
                # Setting one is deliberately out of scope: there is no password
                # reset flow in the product at all.
                raise BusinessRuleError("this account signs in with Google")
            if not verify_password(current_password, current_hash):
                raise InvalidCredentialsError("invalid current password")
            if verify_password(new_password, current_hash):
                raise BusinessRuleError(
                    "new password must be different from current password"
                )

            updated = TableWrite.update_user_password_hash(
                conn, user_id, hash_password(new_password)
            )
            if not updated:
                raise UserNotFoundError()

    # --- helpers --------------------------------------------------------

    def _run_onboarding(self, user_account) -> None:
        timeout = self._settings.tx_confirmations_timeout_s
        self._onchain.fund_gas(
            user_account.address,
            self._settings.signup_gas_grant_wei,
            timeout=timeout,
        )
        self._onchain.faucet_drip(user_account.address, timeout=timeout)
        self._onchain.grant_user_approvals(user_account, timeout=timeout)

    def _maybe_reonboard(self, user: User) -> None:
        """Re-run onboarding for an already-onboarded user with zero native balance.

        Anvil's chain state is wiped on every restart while the DB persists, so a
        user can end up logged in but unfunded. Native balance is the chain-wipe
        signal: on a chain that gets reset it never drops to zero through normal
        use (gas spent per tx is tiny relative to signup_gas_grant_wei). Failures
        here are logged but never block login — the user can still authenticate
        and see balance errors at trade time.

        That reading only holds while the chain is disposable. On a durable chain
        a zero balance means the account spent its gas, and re-granting on login
        would be a treasury faucet anyone could drain on repeat, so
        `simulated_chain=False` turns this off and the signup grant becomes once
        per account. (The house account does not rely on this path at all — it is
        kept above a gas floor by the mirror's top-up loop.)

        A second lock sits beside the first: an account that exported its
        private key while export still existed never gets this repair, because
        its key is out in the wild and a zero balance can also mean the holder
        emptied the wallet on purpose. Wallets are custodial now, so no account
        can newly enter that state, but the ones already in it stay there.
        """
        if not self._settings.simulated_chain:
            return
        with self._db.read() as conn:
            exported_at, _ = TableRead.get_key_export_state(conn, user.user_id)
        if exported_at is not None:
            # While only we hold the key the only way to a zero balance is a
            # chain wipe, which is what this repair is for. A key exported
            # before export was removed can still empty the wallet
            # deliberately, and every login would be another free grant.
            return
        if self._onchain is None or user.onboarded_at is None:
            return
        # `simulated_chain` is only a claim about the chain; the chain id is the
        # fact. A flag left on after a move to a durable chain would otherwise
        # turn every zero-balance login into a free gas grant.
        if not is_disposable_chain(self._onchain.chain_id):
            return
        try:
            native = self._onchain.native_balance(user.eth_address)
        except Exception as exc:
            log.warning("chain balance check failed for %s: %s", user.user_id, exc)
            return
        if native > 0:
            return
        log.info(
            "user %s has zero native balance — re-running onboarding "
            "(chain likely reset)",
            user.user_id,
        )
        try:
            self._run_onboarding(user.eth_key)
        except Exception:
            log.exception("re-onboarding failed for %s", user.user_id)
            return

        # A wipe means the account is starting over: overwrite (not add to)
        # TOTAL_DEPOSITED, or every historical top-up before the wipe would
        # still count as deposited against grant-level post-wipe capital and
        # `earned = capital - deposited` would read deeply negative. Read the
        # balance only after `_run_onboarding` succeeded, so a failed
        # reonboard never resets the figure for an account still on its old
        # grant. Own transaction and swallowed failure, same as the deposit
        # write in register() and for the same reason: this path must keep
        # never blocking login, and a failure here just leaves
        # TOTAL_DEPOSITED at its prior value rather than corrupting it.
        #
        # Record the identity in the same write, exactly as register() does.
        # Without it the row keeps the stale identity even though the deposit
        # was just corrected, so the next top_up finds seen != current and
        # resets the ledger it was just handed back.
        try:
            with self._db.write() as conn:
                TableWrite.set_total_deposited(
                    conn, user.user_id, self._onchain.usd_balance(user.eth_address)
                )
                TableWrite.set_deployment_id(
                    conn, user.user_id, self._onchain.deployment_id
                )
        except Exception:
            log.exception(
                "resetting deposited balance failed for %s", user.user_id
            )

    def _onboard_new_account(self, user_id: str, acct: LocalAccount) -> User:
        """Everything a new account needs once its row exists.

        Both signup paths call this and neither does the work inline. Two copies
        would drift -- one gains a step the other does not -- and the difference
        surfaces months later as an account that cannot trade.

        The row is claimed first (`claim_onboarding`), so two parallel calls
        for one account send the gas grant once and the loser is told to retry.
        """
        now = int(time.time())
        with self._db.write() as conn:
            claimed = TableWrite.claim_onboarding(
                conn, user_id, now, now - self.ONBOARDING_CLAIM_STALE_S
            )
        if not claimed:
            with self._db.read() as conn:
                current = TableRead.get_user_by_userid(conn, user_id)
            if current is None:
                raise UserNotFoundError()
            if current.onboarded_at is not None:
                return current  # someone else finished it
            raise OnboardingError(
                "this account is already being set up — try again in a moment"
            )
        # On-chain onboarding happens *outside* the DB transaction so we don't
        # hold the write lock for ~1s of network round-trips.
        try:
            self._run_onboarding(acct)
        except AdminGasPausedError:
            # Not wrapped: a 503 "try again later", not an OnboardingError (400,
            # or MCP's "still being set up") and not a traceback per sign-in.
            # The claim goes back all the same: the retry the 503 asks for must
            # not find the row held.
            self._release_onboarding_claim(user_id)
            raise
        except Exception as exc:
            log.exception("on-chain onboarding failed for user %s", user_id)
            self._release_onboarding_claim(user_id)
            raise OnboardingError(str(exc)) from exc
        with self._db.write() as conn:
            TableWrite.mark_user_onboarded(conn, user_id)

        # Recording the deposit is a separate transaction from marking the
        # user onboarded above. If it fails -- whether the chain read or the
        # UPDATE itself raises -- psycopg would otherwise issue COMMIT on an
        # already-aborted transaction and Postgres turns that into a
        # ROLLBACK, taking mark_user_onboarded down with it. A failure here
        # must not turn a successful signup into a failed one (same treatment
        # on-chain reads get elsewhere in this service -- see
        # _maybe_reonboard), and TOTAL_DEPOSITED staying NULL is fine: it
        # reads back as the grant via get_total_deposited's default.
        try:
            # Read the granted amount off the chain rather than from config:
            # the grant is baked into an immutable contract by
            # scripts/deploy_exchange.sh, while paper_balance_target_raw is a
            # separate Settings field. They are documented to agree and today
            # they do, but they are two sources and either can move.
            #
            # Read before opening the transaction, for the same reason the
            # onboarding above sits outside one: no DB write lock should be
            # held across a network round-trip.
            granted = self._onchain.usd_balance(acct.address)
            with self._db.write() as conn:
                TableWrite.set_total_deposited(conn, user_id, granted)
                TableWrite.set_deployment_id(
                    conn, user_id, self._onchain.deployment_id
                )
        except Exception:
            log.exception("reading granted balance failed for user %s", user_id)

        with self._db.read() as conn:
            user = TableRead.get_user_by_userid(conn, user_id)
        if user is None:
            raise RuntimeError("user disappeared between insert and read")
        return user

    def _release_onboarding_claim(self, user_id: str) -> None:
        # Swallowed: this runs while the real failure is already on its way to
        # the caller, and a second exception here would replace it. The worst
        # case of not releasing is the stale-claim wait, not a lost error.
        try:
            with self._db.write() as conn:
                TableWrite.release_onboarding_claim(conn, user_id)
        except Exception:
            log.exception("releasing the onboarding claim failed for %s", user_id)

    def _google_response(self, user: User, *, created: bool) -> GoogleAuthResponse:
        issued = self._issue(user)
        return GoogleAuthResponse(
            access_token=issued.access_token,
            token_type=issued.token_type,
            user=issued.user,
            created=created,
        )

    def _issue(self, user: User) -> AuthResponse:
        token = self._coder.encode(user_id=user.user_id, email=user.email)
        return AuthResponse(
            access_token=token,
            user=UserPublic.model_validate(user.model_dump()),
        )
