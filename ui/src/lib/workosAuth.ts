/**
 * The WorkOS redirect flow, as pure functions.
 *
 * `ui/` vitest is node-env with no `@testing-library/react`, so
 * `AuthCallbackPage` cannot be render-tested. Every decision therefore lives
 * here — keep it that way.
 */

/**
 * NOT `<authkit-domain>/oauth2/authorize`.
 *
 * WorkOS advertises both. The `/oauth2/*` endpoints issue tokens whose `iss`
 * is the AuthKit domain; `AuthKitVerifier` on the API pins
 * `https://api.workos.com/user_management/<client_id>`, which is what this
 * flow returns. Taking the more standard-looking path would have every sign-in
 * rejected by the API while the tests, which mint their own tokens, stayed
 * green.
 */
const AUTHORIZE_URL = "https://api.workos.com/user_management/authorize";

/** Trim to null, so a variable set to whitespace means "off" like an unset one.
 *  Read as a value rather than by dynamic key: `import.meta.env` is a typed
 *  interface, and indexing it with a union widens to `any` under
 *  `noImplicitAny` or fails outright.
 *
 *  Exported so this decision — which gates whether the Google button renders
 *  at all — is covered directly, the way `readGoogleClientId` is covered in
 *  `googleAuth.test.ts` rather than only through whatever happens to import
 *  the module. */
export function present(raw: unknown): string | null {
  if (typeof raw !== "string") return null;
  const trimmed = raw.trim();
  return trimmed.length > 0 ? trimmed : null;
}

/** Public by design: it appears in the URL of every sign-in. Absence means
 *  the feature is off and the button must not render, the same rule
 *  `GOOGLE_CLIENT_ID` follows in `googleAuth.ts`. */
export const WORKOS_CLIENT_ID = present(import.meta.env.VITE_WORKOS_CLIENT_ID);

/** Where the state lives between leaving the tab and coming back to it. */
export const STATE_KEY = "agentpit.oauth_state";

/** The route `AuthCallbackPage` is mounted at in `App.tsx`. */
export const CALLBACK_PATH = "/auth/callback";

/**
 * Should the provider hydrate `/me` from whatever token storage already holds?
 *
 * No, when the app has been loaded straight onto the callback route -- and that
 * exception is load-bearing, not tidiness.
 *
 * `AuthCallbackPage` is a descendant of `AuthProvider`, and React runs child
 * effects before parent ones, so the code exchange starts BEFORE the provider's
 * mount hydration. Both are then in flight at once. For every browser returning
 * after the cutover, storage still holds a legacy access token the API no
 * longer accepts and no refresh token to trade for a new one, so the hydration
 * 401s while the exchange succeeds. Two things then destroy the session that
 * just started: the hydration's own catch clears the token, and `apiFetch`
 * dispatches UNAUTHORIZED_EVENT, whose handler sees `tokenRef.current` holding
 * the NEW token and logs out. A completed Google sign-in lands on `/` signed
 * out.
 *
 * Refereeing that after the fact -- comparing tokens in the handler -- fixes
 * only the second path and leaves the first. Not starting the second request is
 * what actually removes the race, and it costs nothing: the callback page's
 * whole job is to establish a session, and it sets the user itself.
 *
 * Matching is case-insensitive because react-router's is: `caseSensitive`
 * defaults to false, so `/Auth/Callback` renders the callback page too, and a
 * check stricter than the router's would reopen the race on that URL.
 */
export function hydratesFromStoredToken(
  pathname: string,
  storedToken: string | null,
): boolean {
  if (!storedToken) return false;
  const normalised = pathname.toLowerCase().replace(/\/+$/, "");
  return normalised !== CALLBACK_PATH;
}

export function buildAuthorizeUrl(params: {
  clientId: string;
  redirectUri: string;
  provider: string;
  state: string;
}): string {
  const url = new URL(AUTHORIZE_URL);
  url.searchParams.set("client_id", params.clientId);
  url.searchParams.set("redirect_uri", params.redirectUri);
  url.searchParams.set("response_type", "code");
  url.searchParams.set("provider", params.provider);
  url.searchParams.set("state", params.state);
  return url.toString();
}

export type CallbackParams = { code: string; state: string } | { error: string };

export function readCallbackParams(search: string): CallbackParams {
  const params = new URLSearchParams(search);
  const error = params.get("error");
  if (error) return { error };
  const code = params.get("code");
  // No code and no error is not an empty sign-in — it is somebody who opened
  // this URL by hand, or a redirect that lost its query string.
  if (!code) return { error: "missing_code" };
  return { code, state: params.get("state") ?? "" };
}

/**
 * Does the state that came back match the one we stored?
 *
 * Empty never matches empty. Without that, a link crafted by somebody else —
 * arriving at a browser that has stored nothing — would compare "" with "" and
 * complete a sign-in in a victim's session.
 */
export function stateMatches(
  returned: string | null,
  stored: string | null,
): boolean {
  if (!returned || !stored) return false;
  return returned === stored;
}

/**
 * Maps a failed `POST /auth/callback` to user-facing copy.
 *
 * Separate from `signInErrorMessage` because nothing here is a code the user
 * typed. What failed is the authorization code WorkOS put in the redirect, and
 * that mapper's wording -- "That code is wrong or expired", "Email sign-in
 * isn't available" -- describes a dialog this person never saw. It also asks
 * them to correct something they cannot: the only recovery from any of these
 * is to start the sign-in again.
 */
export function callbackErrorMessage(status: number): string {
  if (status === 401) {
    // WorkOS refused the exchange: the code was already spent, expired, or
    // issued for another client. Every one of them means "go round again",
    // and none of them is anything the user did wrong.
    return "That sign-in link has already been used or has expired. Please sign in again.";
  }
  if (status === 429) return "Too many attempts. Wait a moment and try again.";
  if (status === 503) {
    // No WorkOS configured on this deployment, or WorkOS was unreachable.
    return "Sign-in isn't available right now. Try again later.";
  }
  return "Could not complete sign-in. Try again in a moment.";
}

/** A fresh state value. `crypto` is injectable so this is testable. */
export function createState(source: Crypto = crypto): string {
  const bytes = new Uint8Array(16);
  source.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}
