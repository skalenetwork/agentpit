import { expect, test } from "bun:test";
import { callbackCode, callbackError, googleUrl, normaliseCode, safeRedirect, sendCodeError, signInError } from "./auth";
import { ApiError } from "./session";

test("a pasted code keeps its first six digits", () => {
  expect(normaliseCode(" 12 34-56 78 ")).toBe("123456");
});

test("errors read as what to do next", () => {
  expect(signInError(new ApiError(401))).toBe("That code is wrong or expired.");
  expect(sendCodeError(new ApiError(422))).toBe("Enter a valid email address.");
  expect(callbackError(new ApiError(503))).toBe("Sign-in is not available right now. Try again later.");
  expect(signInError(new Error("offline"))).toBe("Could not sign you in. Try again in a moment.");
});

test("only a path on this origin is a redirect target", () => {
  expect(safeRedirect("/agents/0xabc?tab=activity")).toBe("/agents/0xabc?tab=activity");
  expect([safeRedirect("//evil.example"), safeRedirect("https://evil.example"), safeRedirect(undefined)]).toEqual([undefined, undefined, undefined]);
});

test("the Google link carries the client, the callback and the state", () => {
  const url = new URL(googleUrl("client_1", "https://app.agentpit.dev", "abc"));
  expect(Object.fromEntries(url.searchParams)).toEqual({
    client_id: "client_1",
    redirect_uri: "https://app.agentpit.dev/auth/callback",
    response_type: "code",
    provider: "GoogleOAuth",
    state: "abc",
  });
});

test("a callback is honoured only with the state this browser stored", () => {
  expect(callbackCode("?code=c1&state=s1", "s1")).toBe("c1");
  expect([callbackCode("?code=c1&state=s1", "s2"), callbackCode("?code=c1&state=s1", null), callbackCode("?code=c1", ""), callbackCode("?error=denied&code=c1&state=s1", "s1")]).toEqual([
    undefined,
    undefined,
    undefined,
    undefined,
  ]);
});
