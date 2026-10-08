"""Auth + onboarding flow tests.

The three legacy doors -- `/register`, `/login`, `/auth/google` -- are gone:
the routes were deleted, so nothing answers them. What is left is the two
credentials that survive the cutover: an AuthKit bearer token, and the
`X-API-Key` header every trading bot uses.

Anvil + the deployed exchange must be running — a first sign-in hits the
faucet and grants approvals as part of creating the account.
"""

import re

from fastapi.testclient import TestClient

from agentpit.api.main import app
from agentpit.auth.passwords import hash_password, verify_password
from agentpit.db.table_read import TableRead
from agentpit.db.table_write import TableWrite
from tests.db_helpers import fresh_test_conn


def _hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ----- the doors that closed --------------------------------------------


def test_the_legacy_doors_are_gone():
    # The routes are deleted, not stubbed, so these are plain 404s. Signing in
    # is the only way an account comes into being.
    with TestClient(app) as client:
        assert client.post("/register", json={}).status_code == 404
        assert client.post("/login", json={}).status_code == 404
        assert client.post("/auth/google", json={}).status_code == 404


def test_the_key_export_doors_are_gone():
    # Wallets are custodial: we are the only holder of every key we generate,
    # which is what lets us pay a user's gas without the holder racing us for
    # it. Deleted, not stubbed -- an existing route would answer an
    # unauthenticated call 401, so a 404 means there is nothing there.
    with TestClient(app) as client:
        assert client.post("/me/private-key/code").status_code == 404
        assert client.post("/me/private-key", json={"code": "123456"}).status_code == 404
    assert [p for p in app.openapi()["paths"] if "private-key" in p] == []


def test_signing_in_never_reads_a_password_hash(monkeypatch, sign_in):
    # The spec's plainest requirement, and the cheapest way to hold it: make
    # reading a hash raise, then drive the only door left. A row's
    # PASSWORD_HASH survives the cutover as a rollback path and must simply go
    # unread by sign-in until plan 4 drops the column. `change_password` still
    # reads it on purpose -- see the /me/password section below.
    def _boom(*_args, **_kwargs):
        raise AssertionError("PASSWORD_HASH was read during sign-in")

    monkeypatch.setattr(TableRead, "get_password_hash_by_userid", _boom)

    with TestClient(app) as client:
        assert sign_in(client, "nohash@example.com")["user"]["api_key"]


# ----- the bearer token that replaced them ------------------------------


def test_me_requires_bearer_token():
    with TestClient(app) as client:
        assert client.get("/me").status_code == 401


def test_me_returns_current_user(sign_in):
    with TestClient(app) as client:
        token = sign_in(client, "eve@example.com")["access_token"]
        resp = client.get("/me", headers=_hdr(token))
        assert resp.status_code == 200
        assert resp.json()["email"] == "eve@example.com"
        # UserPublic is a whitelist and must stay one.
        assert "private_key" not in resp.text
        assert "eth_key" not in resp.text


def test_me_rejects_invalid_token():
    with TestClient(app) as client:
        resp = client.get("/me", headers=_hdr("not.a.real.jwt"))
        assert resp.status_code == 401


def test_patch_me_updates_handle(sign_in):
    with TestClient(app) as client:
        token = sign_in(client, "frank@example.com")["access_token"]
        resp = client.patch("/me", headers=_hdr(token), json={"handle": "frank_1"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["handle"] == "frank_1"


def test_patch_me_rejects_duplicate_handle(sign_in):
    with TestClient(app) as client:
        first = sign_in(client, "grace@example.com")["access_token"]
        client.patch("/me", headers=_hdr(first), json={"handle": "grace"})
        second = sign_in(client, "heidi@example.com")["access_token"]

        resp = client.patch("/me", headers=_hdr(second), json={"handle": "grace"})
        assert resp.status_code == 409


def test_patch_me_rejects_invalid_handle_format(sign_in):
    with TestClient(app) as client:
        token = sign_in(client, "ivan@example.com")["access_token"]
        resp = client.patch("/me", headers=_hdr(token), json={"handle": "not valid"})
        assert resp.status_code == 422


# ----- /me/password, which is live for the accounts that predate the cutover --
#
# No door left open creates a row with a password, but plenty of rows have one:
# `TableWrite.link_workos_identity` preserves PASSWORD_HASH deliberately (see
# its docstring -- it is the rollback), so each of the 17 legacy accounts keeps
# `has_password=True` after its first mailed-code sign-in and is still shown the
# Settings page's password form. So the row is seeded directly and driven over
# `X-API-Key`, which resolves it exactly as it resolves a bot.


def _seed_password_account(email: str, password: str) -> tuple[str, str]:
    """A row that still carries a bcrypt hash. Returns (user_id, api_key)."""
    with fresh_test_conn() as conn:
        user_id, _acct, api_key = TableWrite.create_user(
            conn, email=email, password_hash=hash_password(password), handle=None
        )
    return user_id, api_key


def _stored_hash(user_id: str) -> str | None:
    with fresh_test_conn() as conn:
        return TableRead.get_password_hash_by_userid(conn, user_id)


def test_has_password_reflects_whether_the_row_still_carries_a_hash():
    # The Settings page shows the password form on this flag, so it must not
    # start reporting True for the accounts the cutover creates -- that would
    # put a password form in front of people who have never had one.
    with fresh_test_conn() as conn:
        _uid, _acct, with_hash = TableWrite.create_user(
            conn, email="haspw@example.com", password_hash="$2b$12$x", handle=None
        )
        _uid2, _acct2, without_hash = TableWrite.create_user(
            conn, email="nopw@example.com", password_hash=None, handle=None
        )

    with TestClient(app) as client:
        legacy = client.get("/me", headers={"X-API-Key": with_hash}).json()
        current = client.get("/me", headers={"X-API-Key": without_hash}).json()
    assert legacy["has_password"] is True
    assert current["has_password"] is False


def test_a_legacy_account_can_still_change_its_password():
    # The live success path. It writes through `update_user_password_hash`, so
    # assert the hash actually moved rather than trusting the 200 -- a
    # change_password that validated and then wrote nothing would pass on the
    # status code alone, and the holder would find out at the next sign-in
    # after a revert.
    old, new = "hunter22hunter22", "newhunter22hunter22"
    user_id, api_key = _seed_password_account("legacypw@example.com", old)

    with TestClient(app) as client:
        resp = client.patch(
            "/me/password",
            headers={"X-API-Key": api_key},
            json={"current_password": old, "new_password": new},
        )
    assert resp.status_code == 200, resp.text

    stored = _stored_hash(user_id)
    assert stored is not None
    assert verify_password(new, stored), "the new password did not take"
    assert not verify_password(old, stored), "the old password still works"


def test_changing_a_password_with_the_wrong_current_one_is_401():
    _user_id, api_key = _seed_password_account("wrongpw@example.com", "hunter22hunter22")

    with TestClient(app) as client:
        resp = client.patch(
            "/me/password",
            headers={"X-API-Key": api_key},
            json={
                "current_password": "not-the-current-one",
                "new_password": "newhunter22hunter22",
            },
        )
    assert resp.status_code == 401, resp.text


def test_changing_a_password_to_the_same_one_is_400():
    same = "hunter22hunter22"
    user_id, api_key = _seed_password_account("samepw@example.com", same)
    before = _stored_hash(user_id)

    with TestClient(app) as client:
        resp = client.patch(
            "/me/password",
            headers={"X-API-Key": api_key},
            json={"current_password": same, "new_password": same},
        )
    assert resp.status_code == 400, resp.text
    # bcrypt salts every hash, so a re-hash of the same password would look
    # like a change to any test comparing strings. Assert the column is
    # untouched: the refusal must happen before the write, not after it.
    assert _stored_hash(user_id) == before


def test_changing_a_password_on_a_mailed_code_account_is_refused(sign_in):
    """Nobody signing in *today* has a password to change.

    `change_password` refuses any row with a null PASSWORD_HASH, which is
    every account created since the cutover -- and it does so with the wrong
    reason, "this account signs in with Google", because a null hash used to
    mean exactly that. Pinned rather than fixed: the route survives only so
    that reverting the cutover commit restores a working legacy sign-in, and
    plan 4 deletes it. This test is where a decision to fix the wording
    instead should land.
    """
    with TestClient(app) as client:
        token = sign_in(client, "judy@example.com")["access_token"]
        resp = client.patch(
            "/me/password",
            headers=_hdr(token),
            json={
                "current_password": "hunter22hunter22",
                "new_password": "newhunter22hunter22",
            },
        )
        assert resp.status_code == 400, resp.text


# ----- X-API-Key, which the cutover does not touch ----------------------


def test_signing_in_exposes_an_api_key(sign_in):
    # The bot credential is minted by the account, not by the door it came
    # through, so the mailed-code path has to hand one back the way /register
    # did.
    with TestClient(app) as client:
        body = sign_in(client, "apikey@example.com")
        assert body["user"]["api_key"]


def test_me_accepts_api_key_header(sign_in):
    # The credential every bot trading today authenticates with. Nothing about
    # it moved to WorkOS, and if one assertion in this file survives, it is
    # this one.
    with TestClient(app) as client:
        api_key = sign_in(client, "akauth@example.com")["user"]["api_key"]
        resp = client.get("/me", headers={"X-API-Key": api_key})
        assert resp.status_code == 200, resp.text
        assert resp.json()["email"] == "akauth@example.com"


def test_me_rejects_invalid_api_key():
    with TestClient(app) as client:
        resp = client.get("/me", headers={"X-API-Key": "nope-not-a-key"})
        assert resp.status_code == 401


def test_signing_in_without_a_handle_generates_one(sign_in):
    """A leaderboard of accounts that never set a handle is a column of hex
    strings. The generated name is a starting point -- PATCH /me still
    changes it -- but nobody starts nameless. There is no signup form to type
    one into any more, so generation is the only source there is."""
    with TestClient(app) as client:
        handle = sign_in(client, "nameless@example.com")["user"]["handle"]
        assert handle, "signing in must not leave the handle blank"
        assert re.fullmatch(r"[a-zA-Z0-9_]{1,15}", handle), handle
