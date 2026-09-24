"""The WorkOS surface agentpit uses, and nothing more.

A Protocol rather than the SDK client itself, for one reason: every test in
this repo runs offline, and a service that reaches out to api.workos.com from
a unit test is a test that fails on a train. `FakeWorkOsClient` is the double
every test uses; `RealWorkOsClient` is the only place the SDK is touched.

The surface is deliberately narrow. Plan 2 widens it; this plan needs exactly
what the migration script needs.
"""
import re
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote

import httpx

from agentpit.config import Settings


@dataclass(frozen=True)
class WorkOsUser:
    workos_user_id: str
    email: str
    email_verified: bool


@dataclass(frozen=True)
class WorkOsSession:
    workos_user_id: str
    email: str
    access_token: str
    refresh_token: str


class WorkOsClient(Protocol):
    def create_user(
        self, *, email: str, password_hash: str | None
    ) -> WorkOsUser:
        """Create the user, or return the existing one for this address.

        `password_hash` is a bcrypt hash lifted straight out of our `users`
        table -- WorkOS accepts foreign hashes on create, so an imported
        account signs in with the password it already had and nobody is asked
        to reset anything. `None` is the Google-sourced account that never had
        one.
        """
        ...

    def find_user_by_email(self, email: str) -> WorkOsUser | None:
        ...

    def send_magic_auth_code(self, email: str) -> None:
        """Mail a six-digit code to this address, creating the user if new.

        Returns nothing on purpose. WorkOS hands the code back in the create
        response -- which means the API key alone is enough to sign in as
        anybody, without reading mail -- and the only defence available to us
        is that the value never leaves this method.
        """
        ...

    def authenticate_with_code(self, email: str, code: str) -> WorkOsSession:
        ...

    def authenticate_with_authorization_code(self, code: str) -> WorkOsSession:
        """Exchange the code a provider redirect came back with.

        The `/user_management` flow, NOT the `/oauth2/*` endpoints on the
        AuthKit domain: those issue tokens whose `iss` is the AuthKit domain,
        and `AuthKitVerifier` pins `api.workos.com/user_management/<client_id>`.
        Both are advertised by WorkOS and only one of them is ours.
        """
        ...

    def refresh_session(self, refresh_token: str) -> WorkOsSession:
        ...

    def application_name(self, client_id: str) -> str:
        ...


class WorkOsError(RuntimeError):
    """Anything the WorkOS API refused or failed to answer.

    One type for every failure, deliberately: the migration script catches per
    account and carries on, and it can only do that if it knows what to catch.
    The message never carries the request we sent, and the response body it
    does carry is run through `_redact` first — a traceback from here reaches
    a log, and plan 3 turns this string into a user-visible 401 `detail`.
    """


class WorkOsUnavailableError(WorkOsError):
    """We never reached WorkOS at all — a timeout, a DNS failure, a refused TCP
    connection.

    A subclass rather than a sibling so every `except WorkOsError` (the
    migration script, anything that just wants "the call didn't work") keeps
    catching it unchanged. It exists for one caller: the API, which answers a
    refusal 401 ("your code was wrong") and this 503 ("come back in a moment").
    Collapsing the two made a total sign-in outage look like a wave of typos —
    4xx, so no status-code monitor fires — and told a caller who was never
    mailed anything to request a new code, which loops.
    """


class WorkOsRateLimitedError(WorkOsError):
    """WorkOS refused because we asked too often.

    A subclass rather than a sibling for the same reason
    `WorkOsUnavailableError` is one: `migrate_users` catches `WorkOsError` per
    account and must keep catching this unchanged. It exists for the API, which
    otherwise answers 401 "request a new code" -- instructing a rate-limited
    caller to perform the action that rate limited them, in a loop the UI's own
    resend cooldown cannot break because the cooldown is per dialog, not per
    address.
    """


API_BASE = "https://api.workos.com"

#: bcrypt output: $2a/$2b/$2y, a cost, and the salt+digest. Matched wherever it
#: appears, not anchored, because it is being hunted inside a JSON body.
_BCRYPT_RE = re.compile(r"\$2[aby]?\$\d{2}\$[./A-Za-z0-9]{0,53}")

#: A WorkOS API key: `sk_test_…` / `sk_live_…`. Also matched unanchored, and
#: for the same reason — it is being hunted inside a JSON body, possibly one
#: that has JSON-escaped a quoted copy of our own request.
_API_KEY_RE = re.compile(r"sk_[A-Za-z0-9_]+")


def _redact(text: str, secrets: tuple[str | None, ...] = ()) -> str:
    """Strip anything secret-shaped out of an error body before it is stored.

    The body is kept because it names the field WorkOS objected to, which is
    what makes a failed import diagnosable. But an error response is entitled
    to quote the values it rejected, and we send two kinds of secret:

    - a bcrypt hash lifted from `users.PASSWORD_HASH` on `create_user`.
      `migrate_users` catches per account and calls `log.exception`, so without
      this a rejected import writes a live password hash to stdout next to the
      address it belongs to, during a hand-run production migration.
    - the API key itself, which `/user_management/authenticate` wants in the
      request BODY as `client_secret`. Measured 2026-08-11, WorkOS answers a
      bad code with `{"code": "invalid_code"}` and echoes nothing — but a
      gateway or WAF in front of it is under no such obligation, and that path
      is reached by an unauthenticated caller who supplies the code.

    The two patterns catch those by shape. `secrets` catches the third kind,
    which has no shape to match: the refresh token, an opaque string no regex
    can recognise. It is the longest-lived credential we handle — measured
    2026-08-11, WorkOS does NOT rotate it, so one leaked into a log at WARNING
    keeps working — so the caller passes the literal values it just sent and
    they are removed by identity rather than by pattern.
    """
    for secret in secrets:
        # Guard the empty string: `"".join` semantics would turn `str.replace`
        # into inserting "[redacted]" between every character.
        if secret:
            text = text.replace(secret, "[redacted]")
    return _API_KEY_RE.sub("[redacted]", _BCRYPT_RE.sub("[redacted]", text))


class RealWorkOsClient:
    """The WorkOS REST API over the httpx we already have.

    Not the `workos` SDK, and the reason is a pin: every published version of
    it requires `httpx~=0.28` while this repo runs `httpx[http2]==0.27.0`, the
    client underneath the order-book mirror and the Polymarket sync. The
    current release also wants `cryptography~=50.0` against our `<47`, which
    sits under eth-account's signing. Six REST calls are not worth moving
    either. If the SDK ever becomes worth it, it replaces this class and
    nothing else — that is what the Protocol is for.
    """

    _MAGIC_AUTH_GRANT = "urn:workos:oauth:grant-type:magic-auth:code"

    def __init__(self, api_key: str, client_id: str, *, transport=None):
        self._client_id = client_id
        # Kept beyond the header because /user_management/authenticate wants
        # the same key again in the BODY, under the name `client_secret`.
        self._api_key = api_key
        self._http = httpx.Client(
            base_url=API_BASE,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15.0,
            transport=transport,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        secrets: tuple[str | None, ...] = (),
        **kwargs,
    ) -> dict:
        """`secrets` are the literal secret values in the body we are sending,
        for `_redact` to strip back out if the response quotes them."""
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            # Nothing was reached, so this is not a refusal: `WorkOsUnavailable
            # Error` is what makes the API answer 503 instead of 401 here.
            # Bare `exc` would be fine, but chaining keeps the cause visible in
            # a traceback while the message stays ours.
            raise WorkOsUnavailableError(
                f"WorkOS request failed: {method} {path}"
            ) from exc
        if response.status_code == 429:
            # Split out ahead of the generic refusal below so the API can answer
            # 429 and the already-written UI copy becomes reachable. Same
            # redaction as every other error body.
            raise WorkOsRateLimitedError(
                f"WorkOS {method} {path} returned 429: "
                f"{_redact(response.text, secrets)[:500]}"
            )
        if response.status_code >= 400:
            # The body may name the field WorkOS objected to, which is what
            # makes a failed migration diagnosable. The request is not
            # included: it carries the hash and the key. `_redact` covers the
            # remaining case, a response that quotes the request back at us.
            raise WorkOsError(
                f"WorkOS {method} {path} returned {response.status_code}: "
                f"{_redact(response.text, secrets)[:500]}"
            )
        return response.json()

    @staticmethod
    def _to_user(payload: dict) -> WorkOsUser:
        return WorkOsUser(
            workos_user_id=payload["id"],
            email=payload["email"],
            email_verified=bool(payload.get("email_verified")),
        )

    def create_user(self, *, email: str, password_hash: str | None) -> WorkOsUser:
        existing = self.find_user_by_email(email)
        if existing is not None:
            # Idempotent by construction: the migration script is re-runnable
            # and a second call for the same address must not mint a second
            # identity for one person.
            return existing
        body: dict[str, object] = {"email": email, "email_verified": True}
        if password_hash:
            # Omitted entirely rather than sent as null when absent: a
            # Google-sourced account never had a password, and `null` is a
            # different request from silence.
            body["password_hash"] = password_hash
            body["password_hash_type"] = "bcrypt"
        return self._to_user(
            self._request(
                "POST",
                "/user_management/users",
                json=body,
                secrets=(password_hash,),
            )
        )

    def find_user_by_email(self, email: str) -> WorkOsUser | None:
        page = self._request(
            "GET", "/user_management/users", params={"email": email, "limit": 1}
        )
        for user in page.get("data") or []:
            return self._to_user(user)
        return None

    def send_magic_auth_code(self, email: str) -> None:
        # The response body carries the code. It is deliberately discarded.
        # Measured 2026-08-11: this call returns 201 and CREATES the WorkOS
        # user when the address is new, so there is no separate sign-up call.
        self._request("POST", "/user_management/magic_auth", json={"email": email})

    def _authenticate(self, body: dict) -> WorkOsSession:
        payload = self._request(
            "POST",
            "/user_management/authenticate",
            json={
                "client_id": self._client_id,
                # WorkOS names the API key `client_secret` on this endpoint.
                "client_secret": self._api_key,
                **body,
            },
            # Everything secret this body can carry. The refresh token is the
            # one no pattern can find, and this endpoint is the only place it
            # is ever sent. The six-digit code joins it: it is a live sign-in
            # credential for ten minutes, and this method's errors are logged.
            secrets=(self._api_key, body.get("refresh_token"), body.get("code")),
        )
        return WorkOsSession(
            workos_user_id=payload["user"]["id"],
            email=payload["user"]["email"],
            access_token=payload["access_token"],
            refresh_token=payload["refresh_token"],
        )

    def authenticate_with_code(self, email: str, code: str) -> WorkOsSession:
        return self._authenticate(
            {"grant_type": self._MAGIC_AUTH_GRANT, "code": code, "email": email}
        )

    def authenticate_with_authorization_code(self, code: str) -> WorkOsSession:
        return self._authenticate({"grant_type": "authorization_code", "code": code})

    def refresh_session(self, refresh_token: str) -> WorkOsSession:
        # Measured: WorkOS does NOT rotate the refresh token, so a client that
        # refreshes twice concurrently keeps a working credential either way.
        return self._authenticate(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )

    def application_name(self, client_id: str) -> str:
        return self._request(
            "GET", f"/connect/applications/{quote(client_id, safe='')}"
        )["name"]


class FakeWorkOsClient:
    """In-memory double with the same contract, including the idempotency."""

    def __init__(self) -> None:
        self._by_email: dict[str, WorkOsUser] = {}
        self._next = 1
        self._codes: dict[str, str] = {}
        self._auth_codes: dict[str, str] = {}
        #: How many codes this double has mailed. Doubles as the source of the
        #: code itself, so that no two addresses are ever issued the same one.
        self._codes_sent = 0
        #: Test-only: how many codes were presented for authentication. Lets a
        #: test assert that a request was rejected BEFORE WorkOS was called --
        #: which a status code alone cannot show, since the round trip and the
        #: local rejection both end in the same 4xx.
        self.authenticate_calls = 0
        self.applications: dict[str, str] = {}

    def create_user(self, *, email: str, password_hash: str | None) -> WorkOsUser:
        existing = self.find_user_by_email(email)
        if existing is not None:
            return existing
        user = WorkOsUser(
            workos_user_id=f"user_fake_{self._next}",
            email=email,
            email_verified=True,
        )
        self._next += 1
        self._by_email[email.lower()] = user
        return user

    def find_user_by_email(self, email: str) -> WorkOsUser | None:
        return self._by_email.get(email.lower())

    def send_magic_auth_code(self, email: str) -> None:
        # The real API creates the user on this call; the double must too, or
        # the first-sign-in path is never exercised offline.
        self.create_user(email=email, password_hash=None)
        # Six digits, and a DIFFERENT six for every address: a constant would
        # make the double accept Alice's code presented for Mallory's address,
        # which is exactly one of the cases the sign-in tests have to prove
        # fails. Counter-derived rather than random so failures reproduce.
        self._codes_sent += 1
        self._codes[email.lower()] = f"{100000 + self._codes_sent}"

    def last_code(self, email: str) -> str:
        """Test-only: the code the real API would have mailed."""
        return self._codes[email.lower()]

    def authenticate_with_code(self, email: str, code: str) -> WorkOsSession:
        self.authenticate_calls += 1
        if self._codes.get(email.lower()) != code:
            raise WorkOsError("WorkOS rejected the code")
        user = self.find_user_by_email(email)
        assert user is not None
        return WorkOsSession(
            workos_user_id=user.workos_user_id,
            email=user.email,
            access_token=f"at-{user.workos_user_id}",
            refresh_token=f"rt-{user.workos_user_id}",
        )

    def issue_authorization_code(self, email: str) -> str:
        """Test-only: the code a provider redirect would have come back with."""
        user = self.create_user(email=email, password_hash=None)
        code = f"authcode-{len(self._auth_codes) + 1}-{user.workos_user_id}"
        self._auth_codes[code] = user.workos_user_id
        return code

    def authenticate_with_authorization_code(self, code: str) -> WorkOsSession:
        self.authenticate_calls += 1
        # `pop`, not `get`: WorkOS burns an authorization code on use, and a
        # double that allowed a replay would hide a callback page that posts on
        # every render.
        workos_user_id = self._auth_codes.pop(code, None)
        if workos_user_id is None:
            raise WorkOsError("WorkOS rejected the authorization code")
        for user in self._by_email.values():
            if user.workos_user_id == workos_user_id:
                return WorkOsSession(
                    workos_user_id=user.workos_user_id,
                    email=user.email,
                    access_token=f"at-{workos_user_id}",
                    refresh_token=f"rt-{workos_user_id}",
                )
        raise WorkOsError("WorkOS rejected the authorization code")

    def refresh_session(self, refresh_token: str) -> WorkOsSession:
        if not refresh_token.startswith("rt-"):
            raise WorkOsError("WorkOS rejected the refresh token")
        workos_user_id = refresh_token[3:]
        for user in self._by_email.values():
            if user.workos_user_id == workos_user_id:
                return WorkOsSession(
                    workos_user_id=user.workos_user_id, email=user.email,
                    access_token=f"at-{workos_user_id}",
                    # Measured against the real API: the refresh token does not
                    # rotate, so the caller's existing value stays valid.
                    refresh_token=refresh_token,
                )
        raise WorkOsError("WorkOS rejected the refresh token")

    def application_name(self, client_id: str) -> str:
        return self.applications[client_id]


def build_workos_client(
    settings: Settings, *, transport=None
) -> WorkOsClient | None:
    """The configured client, or None when WorkOS is not set up.

    None is a first-class answer, not a failure: an environment without
    WORKOS_API_KEY is every developer machine until the account exists, and
    startup must not depend on it.

    `transport` exists so tests can drive the REAL client over
    `httpx.MockTransport`. Without it the only code ever exercised offline is
    the double, and the double cannot be wrong about a URL, a header or a
    request body — the three things that are actually easy to get wrong here.
    """
    if not settings.workos_api_key or not settings.workos_client_id:
        return None
    return RealWorkOsClient(
        settings.workos_api_key, settings.workos_client_id, transport=transport
    )
