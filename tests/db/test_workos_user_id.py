from agentpit.config import Settings
from agentpit.db.session import DbSession
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite


def test_set_and_read_back():
    db = DbSession(Settings().database_url)
    with db.write() as conn:
        user_id, _acct, _api_key = TableWrite.create_user(
            conn, email="w@example.com", password_hash="$2b$12$x", handle=None
        )
        assert TableRead.get_user_by_workos_id(conn, "user_01") is None
        assert TableWrite.set_workos_user_id(conn, user_id, "user_01") is True

    with db.read() as conn:
        found = TableRead.get_user_by_workos_id(conn, "user_01")
    assert found is not None
    assert found.user_id == user_id


def test_set_on_a_missing_row_reports_false():
    db = DbSession(Settings().database_url)
    with db.write() as conn:
        assert TableWrite.set_workos_user_id(conn, "nope", "user_02") is False


def test_the_migration_writer_leaves_the_password_alone():
    # The migration backfills ids for accounts nobody has signed into yet, and
    # those accounts must keep logging in with their password until plan 3
    # takes that door away.
    db = DbSession(Settings().database_url)
    with db.write() as conn:
        user_id, _acct, _api_key = TableWrite.create_user(
            conn, email="keep@example.com", password_hash="$2b$12$x", handle=None
        )
        TableWrite.set_workos_user_id(conn, user_id, "user_keep")

    with db.read() as conn:
        assert TableRead.get_password_hash_by_userid(conn, user_id) == "$2b$12$x"


def test_linking_stamps_the_identity_and_keeps_the_password():
    # The sign-in writer, unlike the migration one above. It stamps the
    # identity and LEAVES THE PASSWORD ALONE.
    #
    # Clearing it is the eventual intent -- registration took any address on
    # trust while a mailed code proves it -- but the hash is still a credential
    # somebody can be signed in with: `change_password` reads it, and `/login`
    # is unreachable over HTTP only because the cutover deleted the route, so
    # that reverting one commit restores legacy sign-in. Nulling it here spends
    # that on each holder's first code sign-in, irreversibly, across all 17
    # production accounts. Key export is not the reason -- wallet keys cannot
    # be exported at all -- so the password outlives the routes by one plan
    # and goes when plan 4 drops the column.
    db = DbSession(Settings().database_url)
    with db.write() as conn:
        user_id, _acct, _api_key = TableWrite.create_user(
            conn, email="link@example.com", password_hash="$2b$12$x", handle=None
        )
        assert TableWrite.link_workos_identity(conn, user_id, "user_linked") is True

    with db.read() as conn:
        found = TableRead.get_user_by_workos_id(conn, "user_linked")
        assert found is not None and found.user_id == user_id
        assert TableRead.get_password_hash_by_userid(conn, user_id) == "$2b$12$x"


def test_adoption_leaves_the_password_flag_true():
    """The derived flag, not just the column the test above reads.

    `has_password` is `PASSWORD_HASH IS NOT NULL` projected onto the read
    model, and it is what a caller actually sees. Nothing about the wallet key
    consults it -- keys cannot be exported at all -- but
    `change_password` still needs the hash, and reverting the cutover commit
    puts `/login` back on top of it, so a false here would be the row denying
    a credential it holds.
    """
    db = DbSession(Settings().database_url)
    with db.write() as conn:
        user_id, _acct, _api_key = TableWrite.create_user(
            conn, email="export@example.com", password_hash="$2b$12$y", handle=None
        )
        TableWrite.link_workos_identity(conn, user_id, "user_export")

    with db.read() as conn:
        user = TableRead.get_user_by_workos_id(conn, "user_export")
        assert user is not None
        assert user.has_password is True


def test_linking_a_missing_row_reports_false():
    db = DbSession(Settings().database_url)
    with db.write() as conn:
        assert TableWrite.link_workos_identity(conn, "nope", "user_03") is False


def test_lookup_is_exact_not_fuzzy():
    # A WorkOS id is an opaque token, not an address: it must match exactly or
    # not at all. A LIKE or case-folded lookup here would let one identity
    # answer for another.
    db = DbSession(Settings().database_url)
    with db.write() as conn:
        user_id, _acct, _api_key = TableWrite.create_user(
            conn, email="x@example.com", password_hash=None, handle=None
        )
        TableWrite.set_workos_user_id(conn, user_id, "user_ABC")

    with db.read() as conn:
        assert TableRead.get_user_by_workos_id(conn, "user_abc") is None
        assert TableRead.get_user_by_workos_id(conn, "user_ABC") is not None
