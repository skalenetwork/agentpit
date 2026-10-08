const DEFAULT_BASE_URL = "http://localhost:8000";

const BASE_URL = (
  typeof import.meta.env.VITE_API_BASE_URL === "string" &&
  import.meta.env.VITE_API_BASE_URL.length > 0
    ? import.meta.env.VITE_API_BASE_URL
    : DEFAULT_BASE_URL
).replace(/\/+$/, "");

/** Absolute API origin — exported for the landing page guide's copyable
 *  snippets, so they always target the same host `apiFetch` uses. */
export const API_BASE_URL = BASE_URL;

export class ApiError extends Error {
  readonly status: number;
  readonly body: string;

  constructor(status: number, body: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

/**
 * Auth providers register a getter so apiFetch can read the live token without
 * importing React. Kept as a module-scoped function pointer to avoid a
 * client → context circular import.
 */
type TokenGetter = () => string | null;
let tokenGetter: TokenGetter = () => null;

export function setAccessTokenGetter(getter: TokenGetter): void {
  tokenGetter = getter;
}

/**
 * Auth providers register a refresher so a 401 can be repaired instead of
 * ending the session. Returns the fresh access token, or null when the
 * refresh failed and the session really is over.
 *
 * The AuthKit access token lives 300 seconds (measured against staging,
 * 2026-08-11). Without this, a signed-in user is logged out every five
 * minutes. Registered as a function pointer for the same reason as the token
 * getter above: no client → context circular import.
 */
type TokenRefresher = () => Promise<string | null>;
let tokenRefresher: TokenRefresher | null = null;

export function setTokenRefresher(refresher: TokenRefresher | null): void {
  tokenRefresher = refresher;
}

/** Event name dispatched on window when a 401 hits with a token attached. */
export const UNAUTHORIZED_EVENT = "agentpit:unauthorized";

/**
 * `RequestInit` plus an opt-out from the UNAUTHORIZED_EVENT dispatch below.
 *
 * Default (unset/false) keeps today's behaviour for every existing caller: a
 * 401 with a token attached means the session died, so we log out. Only a
 * caller re-authenticating with the account's OWN factor (a password re-auth
 * prompt, not a bearer-token check) should set this — there a 401 means "you
 * typed it wrong," the expected case, not "your session died."
 */
export type ApiFetchInit = RequestInit & { skipAuthEvent?: boolean | undefined };

function send(
  url: string,
  init: ApiFetchInit | undefined,
  token: string | null,
): Promise<Response> {
  const baseHeaders: Record<string, string> = { Accept: "application/json" };
  if (init?.body && typeof init.body === "string") {
    baseHeaders["Content-Type"] = "application/json";
  }
  if (token) {
    baseHeaders.Authorization = `Bearer ${token}`;
  }
  return fetch(url, {
    ...init,
    headers: {
      ...baseHeaders,
      ...init?.headers,
    },
  });
}

export async function apiFetch<T>(
  path: string,
  init?: ApiFetchInit,
): Promise<T> {
  const url = `${BASE_URL}${path.startsWith("/") ? path : `/${path}`}`;
  const token = tokenGetter();

  let response = await send(url, init, token);

  if (
    response.status === 401 &&
    token &&
    !init?.skipAuthEvent &&
    tokenRefresher
  ) {
    // The access token expired mid-session. Refresh once and replay, rather
    // than throwing the user out — a 300-second token otherwise ends the
    // session every five minutes.
    //
    // skipAuthEvent callers are excluded on purpose: `/auth/session` and
    // `/auth/refresh` answer 401 in normal use, and the refresher is written
    // in terms of them, so refreshing there would recurse.
    //
    // Replaying reuses `init` as given. Every caller in this codebase sends a
    // string body or none; a stream body would already be consumed and must
    // not be routed through here.
    const fresh = await tokenRefresher().catch(() => null);
    if (fresh) {
      // Exactly one retry. If the fresh token is rejected too, the 401 falls
      // through below and ends the session, which is the honest outcome.
      response = await send(url, init, fresh);
    }
  }

  if (!response.ok) {
    const body = await response.text().catch(() => "");
    if (response.status === 401 && token && !init?.skipAuthEvent) {
      // Server rejected our token (expired, secret rotated, account deleted).
      // The provider listens for this and clears local auth state.
      //
      // A password re-auth endpoint (change-password) also answers 401
      // for "you typed the wrong password" — nothing to do with
      // the bearer token above, which is still perfectly valid. Those
      // callers pass skipAuthEvent so a mistyped password doesn't log the
      // user out from under the dialog they're re-authenticating in. Do not
      // remove this opt-out to "simplify" the check.
      window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT));
    }
    throw new ApiError(
      response.status,
      body,
      `Request failed: ${response.status} ${response.statusText}${
        body ? ` — ${body.slice(0, 200)}` : ""
      }`,
    );
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}
