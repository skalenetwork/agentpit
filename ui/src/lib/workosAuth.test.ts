import { describe, expect, it } from "vitest";
import {
  buildAuthorizeUrl,
  CALLBACK_PATH,
  callbackErrorMessage,
  createState,
  hydratesFromStoredToken,
  present,
  readCallbackParams,
  stateMatches,
} from "./workosAuth";
import { signInErrorMessage } from "@/components/auth/codeFlow";

describe("present", () => {
  // Mirrors `readGoogleClientId` in `googleAuth.ts` — same rule, same cases,
  // because this is the function that decides whether `WORKOS_CLIENT_ID`
  // counts as set, which gates whether the Google button renders at all in
  // `AuthDialog`.
  it("returns the value when set", () => {
    expect(present("client_1")).toBe("client_1");
  });

  it("treats a non-string as off", () => {
    expect(present(undefined)).toBeNull();
    expect(present(null)).toBeNull();
  });

  it("treats an empty or blank string as off", () => {
    // A build arg that resolved to nothing must switch the feature off, not
    // send WorkOS a `client_id` of spaces.
    expect(present("")).toBeNull();
    expect(present("   ")).toBeNull();
  });

  it("trims surrounding whitespace", () => {
    expect(present(" client_1 \n")).toBe("client_1");
  });
});

describe("hydratesFromStoredToken", () => {
  // This is the whole of the fix for the callback race, held out here because
  // `ui/` vitest is node-env and cannot render AuthProvider to catch it.
  it("hydrates on an ordinary route when a token is stored", () => {
    expect(hydratesFromStoredToken("/markets", "tok")).toBe(true);
  });

  it("does not hydrate with nothing stored", () => {
    expect(hydratesFromStoredToken("/markets", null)).toBe(false);
    expect(hydratesFromStoredToken("/markets", "")).toBe(false);
  });

  it("does not hydrate on the callback route even holding a token", () => {
    // The case the cutover produces for every returning browser: a legacy
    // token in storage that the API no longer accepts. Hydrating it races the
    // code exchange and ends with the user logged out of the session the
    // exchange just created.
    expect(hydratesFromStoredToken(CALLBACK_PATH, "stale-legacy-jwt")).toBe(
      false,
    );
  });

  it("ignores a trailing slash on the callback route", () => {
    expect(hydratesFromStoredToken("/auth/callback/", "tok")).toBe(false);
  });

  it("ignores case, because react-router does", () => {
    // `caseSensitive` defaults to false, so this URL renders the callback page
    // as well — a stricter check here would leave the race open on it.
    expect(hydratesFromStoredToken("/Auth/Callback", "tok")).toBe(false);
  });

  it("still hydrates on a route that merely starts the same way", () => {
    expect(hydratesFromStoredToken("/auth/callback-help", "tok")).toBe(true);
  });
});

describe("callbackErrorMessage", () => {
  it("describes a spent or expired redirect, not a typed code", () => {
    const message = callbackErrorMessage(401);
    // The user typed nothing on this page — copy about a wrong code sends
    // them looking for a dialog they never opened.
    expect(message).not.toMatch(/wrong/i);
    expect(message).toMatch(/sign in again/i);
  });

  it("does not call the outage 'email sign-in'", () => {
    // 503 here is the redirect exchange failing; the user never chose email.
    const message = callbackErrorMessage(503);
    expect(message).not.toMatch(/email/i);
    expect(message).toMatch(/isn't available/i);
  });

  it("keeps 429 distinct from a refusal", () => {
    expect(callbackErrorMessage(429)).not.toBe(callbackErrorMessage(401));
    expect(callbackErrorMessage(429)).toMatch(/too many/i);
  });

  it("falls back to generic copy for anything else, including no response", () => {
    expect(callbackErrorMessage(500)).toBe(
      "Could not complete sign-in. Try again in a moment.",
    );
    expect(callbackErrorMessage(0)).toBe(
      "Could not complete sign-in. Try again in a moment.",
    );
  });

  it("differs from the typed-code mapper wherever the wording is about a code", () => {
    // The whole reason this exists: `signInErrorMessage` was being reused
    // here, and its wording describes the dialog rather than the redirect.
    // 429 is deliberately excluded — a rate limit reads the same either way,
    // and forcing it to differ would be churn for its own sake.
    for (const status of [401, 503, 500, 0]) {
      expect(callbackErrorMessage(status)).not.toBe(signInErrorMessage(status));
    }
    expect(callbackErrorMessage(429)).toBe(signInErrorMessage(429));
  });
});

describe("buildAuthorizeUrl", () => {
  it("targets the user_management authorize endpoint, not oauth2", () => {
    // The `/oauth2/authorize` endpoint on the AuthKit domain issues tokens
    // with a different `iss` than AuthKitVerifier pins, so every sign-in
    // through it would be rejected by the API.
    const url = buildAuthorizeUrl({
      clientId: "client_1",
      redirectUri: "http://localhost:5173/auth/callback",
      provider: "GoogleOAuth",
      state: "st",
    });
    expect(url).toContain("https://api.workos.com/user_management/authorize");
    expect(url).not.toContain("/oauth2/");
  });

  it("carries the client id, redirect, provider, state and response type", () => {
    const url = new URL(
      buildAuthorizeUrl({
        clientId: "client_1",
        redirectUri: "http://localhost:5173/auth/callback",
        provider: "GoogleOAuth",
        state: "st",
      }),
    );
    expect(url.searchParams.get("client_id")).toBe("client_1");
    expect(url.searchParams.get("redirect_uri")).toBe(
      "http://localhost:5173/auth/callback",
    );
    expect(url.searchParams.get("provider")).toBe("GoogleOAuth");
    expect(url.searchParams.get("state")).toBe("st");
    expect(url.searchParams.get("response_type")).toBe("code");
  });
});

describe("readCallbackParams", () => {
  it("reads a code and state", () => {
    expect(readCallbackParams("?code=abc&state=st")).toEqual({
      code: "abc",
      state: "st",
    });
  });

  it("reports the provider's error instead of a code", () => {
    expect(readCallbackParams("?error=access_denied")).toEqual({
      error: "access_denied",
    });
  });

  it("treats a missing code as an error rather than an empty sign-in", () => {
    expect(readCallbackParams("")).toEqual({ error: "missing_code" });
  });
});

describe("stateMatches", () => {
  it("is true only for an exact match", () => {
    expect(stateMatches("st", "st")).toBe(true);
    expect(stateMatches("st", "other")).toBe(false);
  });

  it("is false when either side is missing", () => {
    // A link crafted by somebody else arrives with no stored state. Accepting
    // that completes a sign-in in a victim's browser.
    expect(stateMatches("st", null)).toBe(false);
    expect(stateMatches(null, "st")).toBe(false);
    expect(stateMatches(null, null)).toBe(false);
  });

  it("is false for the empty string on both sides", () => {
    expect(stateMatches("", "")).toBe(false);
  });
});

describe("createState", () => {
  it("returns 16 bytes as lowercase hex, from whatever source it's given", () => {
    // Deterministic in shape only — the value itself must not be asserted,
    // or the test would just be re-deriving the fill pattern below.
    const fakeCrypto = {
      getRandomValues: (bytes: Uint8Array) => {
        bytes.forEach((_, i) => {
          bytes[i] = i;
        });
        return bytes;
      },
    } as unknown as Crypto;

    const state = createState(fakeCrypto);
    expect(state).toHaveLength(32);
    expect(state).toMatch(/^[0-9a-f]{32}$/);
  });

  it("asks the injected source for randomness rather than always using the global", () => {
    let calls = 0;
    const fakeCrypto = {
      getRandomValues: (bytes: Uint8Array) => {
        calls += 1;
        return bytes;
      },
    } as unknown as Crypto;

    createState(fakeCrypto);
    expect(calls).toBe(1);
  });
});
