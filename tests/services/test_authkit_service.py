import pytest

from agentpit.auth.workos_client import FakeWorkOsClient, WorkOsError
from agentpit.config import Settings
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from agentpit.domain.exceptions import InvalidCredentialsError, OnboardingError
from agentpit.services.authkit_service import AuthKitService


class _Onboarder:
    """Stands in for AuthService._onboard_new_account.

    The real one funds gas, drips collateral and sends three approvals -- a
    second or so of chain round-trips. These tests are about identity, so the
    chain is a spy: what matters is that it is called exactly once per NEW
    account and never for a returning one.
    """

    def __init__(self):
        self.calls = []

    def __call__(self, user_id, acct):
        self.calls.append(user_id)
        with DbSession(Settings().database_url).write() as conn:
            TableWrite.mark_user_onboarded(conn, user_id)
            return TableRead.get_user_by_userid(conn, user_id)


class _FlakyOnboarder(_Onboarder):
    """Onboarding that fails its first `failures` calls, as a restarting anvil
    makes the real one fail: `AuthService._onboard_new_account` turns any chain
    exception into `OnboardingError`, after the row has already committed."""

    def __init__(self, failures: int = 1):
        super().__init__()
        self._failures = failures

    def __call__(self, user_id, acct):
        if self._failures > 0:
            self._failures -= 1
            self.calls.append(user_id)
            raise OnboardingError("chain is restarting")
        return super().__call__(user_id, acct)


class _Reonboarder:
    """Stands in for AuthService._maybe_reonboard.

    The real one reads a native balance off the chain and re-funds a wallet the
    chain forgot. Here it only records that it was asked, because what these
    tests are about is WHICH sign-in paths ask.
    """

    def __init__(self):
        self.calls = []

    def __call__(self, user):
        self.calls.append(user.user_id)




def _service(workos=None, onboarder=None, reonboarder=None):
    db = DbSession(Settings().database_url)
    return AuthKitService(
        db=db,
        workos=workos or FakeWorkOsClient(),
        onboard=onboarder or _Onboarder(),
        reonboard=reonboarder or _Reonboarder(),
    ), db


def test_first_sign_in_creates_the_account_and_onboards_it():
    workos, onboarder = FakeWorkOsClient(), _Onboarder()
    svc, db = _service(workos, onboarder)

    svc.send_code("new@example.com")
    session = svc.sign_in("new@example.com", workos.last_code("new@example.com"))

    assert session.user.email == "new@example.com"
    assert session.user.handle  # generated, never blank
    assert session.access_token and session.refresh_token
    assert onboarder.calls == [session.user.user_id]

    with db.read() as conn:
        linked = TableRead.get_user_by_workos_id(
            conn, workos.find_user_by_email("new@example.com").workos_user_id
        )
    assert linked is not None and linked.user_id == session.user.user_id


def test_a_returning_address_lands_on_the_same_row_without_a_second_wallet():
    # The failure this guards is expensive and silent: a second row means a
    # second wallet, a second onboarding paid for on chain, and a person whose
    # positions have vanished.
    workos, onboarder = FakeWorkOsClient(), _Onboarder()
    svc, _db = _service(workos, onboarder)

    svc.send_code("a@example.com")
    first = svc.sign_in("a@example.com", workos.last_code("a@example.com"))
    svc.send_code("a@example.com")
    second = svc.sign_in("a@example.com", workos.last_code("a@example.com"))

    assert first.user.user_id == second.user.user_id
    assert first.user.eth_address == second.user.eth_address
    assert onboarder.calls == [first.user.user_id]  # onboarded once, not twice


def test_an_account_migrated_by_workos_user_id_is_found_not_recreated():
    # Plan 1's migration script populated WORKOS_USER_ID for the existing
    # accounts. Those people must sign in to their own rows.
    workos, onboarder = FakeWorkOsClient(), _Onboarder()
    svc, db = _service(workos, onboarder)
    with db.write() as conn:
        user_id, _acct, _key = TableWrite.create_user(
            conn, email="old@example.com", password_hash="$2b$12$x", handle=None
        )
        # An account the migration touched is one that registered and onboarded
        # long ago. Without the stamp this row is not a migrated account but a
        # stranded one, which sign-in is now expected to finish onboarding —
        # see test_onboarding_that_failed_on_the_first_sign_in_is_finished_later.
        TableWrite.mark_user_onboarded(conn, user_id)
    created = workos.create_user(email="old@example.com", password_hash=None)
    with db.write() as conn:
        TableWrite.set_workos_user_id(conn, user_id, created.workos_user_id)

    workos.send_magic_auth_code("old@example.com")
    session = svc.sign_in("old@example.com", workos.last_code("old@example.com"))

    assert session.user.user_id == user_id
    assert onboarder.calls == []  # already had a wallet


def _legacy_password_row(db, email: str) -> str:
    """A row as `/register` leaves one: a password, no WORKOS_USER_ID, onboarded.

    `/register` stays live through this plan, so these keep appearing right up
    until plan 3, and their owners then sign in by code for the first time.
    """
    with db.write() as conn:
        user_id, _acct, _key = TableWrite.create_user(
            conn, email=email, password_hash="$2b$12$x", handle=None
        )
        TableWrite.mark_user_onboarded(conn, user_id)
    return user_id


def test_a_legacy_password_row_is_adopted_rather_than_duplicated():
    # `create_user` would hit `EMAIL TEXT NOT NULL UNIQUE` and turn every
    # sign-in by this person into a 500 -- or, without the constraint, hand
    # them a second wallet and hide the positions they already hold.
    workos, onboarder = FakeWorkOsClient(), _Onboarder()
    svc, db = _service(workos, onboarder)
    user_id = _legacy_password_row(db, "bob@corp.com")

    svc.send_code("bob@corp.com")
    session = svc.sign_in("bob@corp.com", workos.last_code("bob@corp.com"))

    assert session.user.user_id == user_id
    assert onboarder.calls == []  # it already has a wallet
    with db.read() as conn:
        linked = TableRead.get_user_by_workos_id(
            conn, workos.find_user_by_email("bob@corp.com").workos_user_id
        )
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE LOWER(EMAIL) = %s",
            ("bob@corp.com",),
        ).fetchone()["n"]
    assert linked is not None and linked.user_id == user_id
    assert rows == 1


def test_adopting_a_legacy_row_leaves_its_password_alone():
    """Adoption must not strip the hash — not yet, and not like this.

    Clearing it is the eventual intent, and the reasoning is sound: nobody ever
    verified that whoever set that password owns the address, while a mailed
    code proves it. What stands in the way is not key export — wallet keys
    cannot be exported at all. It is the rollback: `change_password` still reads this hash, and `/login` answers
    410 only because the cutover left the service under it intact, so
    reverting that one commit restores legacy sign-in — over these hashes.
    Stripping the hash here spends that credential account by account — all 17
    production accounts have one — on each holder's first mailed-code sign-in,
    permanently, because the hash is not recoverable.

    The password outlives the door by one plan: the legacy routes are gone
    now, and plan 4 drops the column. Until then, the password stays.
    """
    workos, onboarder = FakeWorkOsClient(), _Onboarder()
    svc, db = _service(workos, onboarder)
    user_id = _legacy_password_row(db, "bob@corp.com")

    svc.send_code("bob@corp.com")
    session = svc.sign_in("bob@corp.com", workos.last_code("bob@corp.com"))

    assert session.user.user_id == user_id
    with db.read() as conn:
        assert TableRead.get_password_hash_by_userid(conn, user_id) is not None
    # And the session says so: the flag is `PASSWORD_HASH IS NOT NULL`, so the
    # response must not deny a credential the row still holds.
    assert session.user.has_password is True


def test_a_legacy_row_is_matched_case_insensitively():
    # Registration stores the address as typed; WorkOS reports a normalised
    # one. This is the one place that difference would mint a second wallet.
    workos, onboarder = FakeWorkOsClient(), _Onboarder()
    svc, db = _service(workos, onboarder)
    user_id = _legacy_password_row(db, "Alice@Corp.com")

    svc.send_code("alice@corp.com")
    session = svc.sign_in("alice@corp.com", workos.last_code("alice@corp.com"))

    assert session.user.user_id == user_id
    assert onboarder.calls == []


def test_onboarding_that_failed_on_the_first_sign_in_is_finished_later():
    # The row and its WORKOS_USER_ID commit before onboarding runs -- chain
    # round-trips must not hold the write lock -- so a chain that is down
    # during somebody's first sign-in strands them with a wallet holding no
    # gas, no collateral and no approvals. Every later sign-in would find that
    # row by WorkOS id, and `AuthService._maybe_reonboard` bails precisely when
    # ONBOARDED_AT is NULL, so nothing else repairs it.
    workos, onboarder = FakeWorkOsClient(), _FlakyOnboarder()
    svc, db = _service(workos, onboarder)

    svc.send_code("new@example.com")
    with pytest.raises(OnboardingError):
        svc.sign_in("new@example.com", workos.last_code("new@example.com"))
    with db.read() as conn:
        stranded = TableRead.get_user_by_email_ci(conn, "new@example.com")
    assert stranded is not None and stranded.onboarded_at is None

    svc.send_code("new@example.com")
    session = svc.sign_in("new@example.com", workos.last_code("new@example.com"))

    assert session.user.user_id == stranded.user_id  # the same row, not a second
    assert session.user.onboarded_at is not None
    assert onboarder.calls == [stranded.user_id, stranded.user_id]


def test_a_stranded_legacy_row_is_finished_on_adoption_too():
    # Same repair on the other door in: a /register whose own onboarding failed
    # leaves a password row with no wallet, and adopting it must not hand back
    # a session for one.
    workos, onboarder = FakeWorkOsClient(), _Onboarder()
    svc, db = _service(workos, onboarder)
    with db.write() as conn:
        user_id, _acct, _key = TableWrite.create_user(
            conn, email="bob@corp.com", password_hash="$2b$12$x", handle=None
        )

    svc.send_code("bob@corp.com")
    session = svc.sign_in("bob@corp.com", workos.last_code("bob@corp.com"))

    assert session.user.user_id == user_id
    assert session.user.onboarded_at is not None
    assert onboarder.calls == [user_id]


def test_a_wrong_code_creates_nothing():
    workos, onboarder = FakeWorkOsClient(), _Onboarder()
    svc, db = _service(workos, onboarder)
    svc.send_code("a@example.com")

    with pytest.raises(WorkOsError):
        svc.sign_in("a@example.com", "000000")

    # The whole point of the design: an address that never answers costs us
    # no row, no wallet and no gas.
    with db.read() as conn:
        assert TableRead.get_user_by_email(conn, "a@example.com") is None
    assert onboarder.calls == []


def test_refresh_returns_the_same_account():
    workos = FakeWorkOsClient()
    svc, _db = _service(workos)
    svc.send_code("a@example.com")
    first = svc.sign_in("a@example.com", workos.last_code("a@example.com"))

    again = svc.refresh(first.refresh_token)
    assert again.user.user_id == first.user.user_id
    assert again.access_token


def test_refresh_for_an_identity_with_no_local_row_is_refused():
    # A valid WorkOS session whose `sub` matches nothing here must not
    # silently mint an account -- account creation belongs to sign_in alone.
    workos = FakeWorkOsClient()
    svc, _db = _service(workos)
    created = workos.create_user(email="ghost@example.com", password_hash=None)
    # The refusal by name, not `Exception`: with the broad one an AttributeError
    # from a renamed field would pass just as happily, and the claim above --
    # that this is a refusal rather than a crash -- would go unpinned.
    with pytest.raises(InvalidCredentialsError):
        svc.refresh(f"rt-{created.workos_user_id}")


def test_refresh_never_runs_on_chain_onboarding():
    # A background call every 300 seconds with nobody watching. Onboarding is
    # ~a second of chain round-trips and real gas, and the whole page's
    # requests queue behind the shared in-flight refresh while it waits. If it
    # failed once it will most likely fail again, so retrying it on a timer
    # buys nothing and costs every five minutes.
    workos, onboarder = FakeWorkOsClient(), _Onboarder()
    svc, db = _service(workos, onboarder)
    svc.send_code("a@example.com")
    first = svc.sign_in("a@example.com", workos.last_code("a@example.com"))

    with db.write() as conn:
        TableWrite.clear_user_onboarded(conn, first.user.user_id)
    onboarder.calls.clear()

    again = svc.refresh(first.refresh_token)

    assert again.user.user_id == first.user.user_id
    assert onboarder.calls == []


def test_sign_in_finishes_an_onboarding_that_never_completed():
    # The complement of the test above. `_create_account` commits the row
    # before onboarding it, so a chain that was down during somebody's first
    # sign-in leaves a committed row with ONBOARDED_AT null -- a wallet with no
    # gas, no collateral and no approvals, failing every order at trade time.
    workos, onboarder = FakeWorkOsClient(), _Onboarder()
    svc, db = _service(workos, onboarder)
    svc.send_code("b@example.com")
    first = svc.sign_in("b@example.com", workos.last_code("b@example.com"))

    with db.write() as conn:
        TableWrite.clear_user_onboarded(conn, first.user.user_id)
    onboarder.calls.clear()

    svc.send_code("b@example.com")
    svc.sign_in("b@example.com", workos.last_code("b@example.com"))

    assert onboarder.calls == [first.user.user_id]


def test_sign_in_runs_the_chain_wipe_repair_for_an_onboarded_account():
    # `_maybe_reonboard` hangs off `login` and `google_sign_in` and has never
    # been reachable from a mailed-code sign-in. On the local anvil, whose
    # state is wiped on restart while the database persists, whoever signs in
    # by code therefore stays unfunded forever.
    workos, reonboarder = FakeWorkOsClient(), _Reonboarder()
    svc, _db = _service(workos, reonboarder=reonboarder)
    svc.send_code("c@example.com")
    first = svc.sign_in("c@example.com", workos.last_code("c@example.com"))

    svc.send_code("c@example.com")
    second = svc.sign_in("c@example.com", workos.last_code("c@example.com"))

    # Not on the first: that account was onboarded by this very call and its
    # wallet is as funded as it will ever be.
    assert reonboarder.calls == [second.user.user_id]
    assert first.user.user_id == second.user.user_id


def test_a_row_that_appears_after_the_link_lookup_is_adopted_not_500(monkeypatch):
    # The window `_link_existing_account` cannot close: it looks, finds
    # nothing, and the winner commits before our insert runs. Simulated by
    # creating the row from inside the lookup itself.
    workos = FakeWorkOsClient()
    svc, db = _service(workos)
    svc.send_code("late@example.com")
    code = workos.last_code("late@example.com")
    created = workos.find_user_by_email("late@example.com")

    original = svc._link_existing_account
    winner = {}

    def _link_then_race(session):
        result = original(session)
        if not winner:
            with db.write() as conn:
                user_id, _acct, _key = TableWrite.create_user(
                    conn, email=session.email, password_hash=None, handle=None
                )
                TableWrite.set_workos_user_id(
                    conn, user_id, created.workos_user_id
                )
            winner["user_id"] = user_id
        return result

    monkeypatch.setattr(svc, "_link_existing_account", _link_then_race)

    session = svc.sign_in("late@example.com", code)

    assert session.user.user_id == winner["user_id"]


def test_refresh_never_runs_the_chain_wipe_repair():
    workos, reonboarder = FakeWorkOsClient(), _Reonboarder()
    svc, _db = _service(workos, reonboarder=reonboarder)
    svc.send_code("d@example.com")
    first = svc.sign_in("d@example.com", workos.last_code("d@example.com"))
    reonboarder.calls.clear()

    svc.refresh(first.refresh_token)

    assert reonboarder.calls == []


# --- sign_in_with_authorization_code: a provider redirect landing back on us -


def test_a_first_authorization_code_creates_the_account_and_onboards_it():
    workos, onboarder = FakeWorkOsClient(), _Onboarder()
    svc, _db = _service(workos, onboarder)

    code = workos.issue_authorization_code("g@example.com")
    session = svc.sign_in_with_authorization_code(code)

    assert session.user.email == "g@example.com"
    assert session.user.eth_address.startswith("0x")
    assert onboarder.calls == [session.user.user_id]


def test_a_google_account_that_already_exists_here_is_adopted_not_duplicated():
    # Today's Google users have a row with GOOGLE_SUB and no password. Coming
    # back through the WorkOS redirect they must land on it: a second row is a
    # second wallet and a person whose positions have disappeared.
    #
    # Marked onboarded, like every other "already exists here" row in this
    # file (see `_legacy_password_row`): a Google account that signed in
    # before already has a funded wallet, and `onboarder.calls == []` below is
    # what proves adoption doesn't redo that work. The un-onboarded case --
    # a row stranded mid-onboarding -- has its own test:
    # `test_a_stranded_legacy_row_is_finished_on_adoption_too`.
    workos, onboarder = FakeWorkOsClient(), _Onboarder()
    svc, db = _service(workos, onboarder)
    with db.write() as conn:
        user_id, _acct, _key = TableWrite.create_user(
            conn, email="old@example.com", password_hash=None, handle=None,
            google_sub="google-sub-1",
        )
        TableWrite.mark_user_onboarded(conn, user_id)

    code = workos.issue_authorization_code("old@example.com")
    session = svc.sign_in_with_authorization_code(code)

    assert session.user.user_id == user_id
    assert onboarder.calls == []
