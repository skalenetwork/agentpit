import { ApiError } from "./session";

const AUTHORIZE = "https://api.workos.com/user_management/authorize";
const STATE = "agentpit.oauth_state";
const REDIRECT = "agentpit.oauth_redirect";
const clientId: string | undefined = import.meta.env.VITE_WORKOS_CLIENT_ID?.trim() || undefined;

export const CODE_LENGTH = 6;

export const normaliseCode = (raw: string) => raw.replace(/\D+/g, "").slice(0, CODE_LENGTH);

const fault = (specific: Record<number, string>, fallback: string) => (error: unknown) =>
  ({ 429: "Too many attempts. Wait a moment and try again.", 503: "Sign-in is not available right now. Try again later.", ...specific })[error instanceof ApiError ? error.status : 0] ?? fallback;

export const sendCodeError = fault({ 422: "Enter a valid email address." }, "Could not send the code. Try again in a moment.");
export const signInError = fault({ 401: "That code is wrong or expired." }, "Could not sign you in. Try again in a moment.");
export const callbackError = fault({ 401: "That sign-in link was already used or has expired." }, "Could not complete sign-in. Try again in a moment.");

export const safeRedirect = (target: unknown) => (typeof target === "string" && /^\/(?!\/)/.test(target) ? target : undefined);

export const googleUrl = (client: string, origin: string, state: string) =>
  `${AUTHORIZE}?${new URLSearchParams({ client_id: client, redirect_uri: `${origin}/auth/callback`, response_type: "code", provider: "GoogleOAuth", state })}`;

export const startGoogle = clientId
  ? (redirect: string | undefined) => {
      const state = Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, "0")).join("");
      sessionStorage.setItem(STATE, state);
      sessionStorage.setItem(REDIRECT, redirect ?? "/");
      location.assign(googleUrl(clientId, location.origin, state));
    }
  : undefined;

export const callbackCode = (search: string, stored: string | null) => {
  const params = new URLSearchParams(search);
  const code = params.get("code");
  return !params.get("error") && code && stored && params.get("state") === stored ? code : undefined;
};

const take = (key: string) => {
  const value = sessionStorage.getItem(key);
  sessionStorage.removeItem(key);
  return value;
};

export const takeState = () => take(STATE);
export const takeRedirect = () => safeRedirect(take(REDIRECT)) ?? "/";
