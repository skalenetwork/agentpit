import { apiFetch } from "./client";

export type UserPublic = {
  user_id: string;
  email: string;
  handle: string | null;
  eth_address: string;
  api_key: string;
  has_password: boolean;
  onboarded_at: number | null;
  created_at: number;
  auto_redeem: boolean;
};

export type AuthResponse = {
  access_token: string;
  token_type: "bearer";
  /**
   * Null on the legacy `/register` and `/login` paths, which issue a 24-hour
   * token with nothing to refresh. Only the mailed-code paths fill it in.
   */
  refresh_token: string | null;
  user: UserPublic;
};

export function loginRequest(
  email: string,
  password: string,
): Promise<AuthResponse> {
  return apiFetch<AuthResponse>("/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
}

export function registerRequest(
  email: string,
  password: string,
): Promise<AuthResponse> {
  return apiFetch<AuthResponse>("/register", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
}

export type GoogleAuthResponse = AuthResponse & {
  /** True when this sign-in created the account. */
  created: boolean;
};

export function googleSignInRequest(
  credential: string,
): Promise<GoogleAuthResponse> {
  return apiFetch<GoogleAuthResponse>("/auth/google", {
    method: "POST",
    body: JSON.stringify({ credential }),
  });
}

/**
 * Ask WorkOS to mail a six-digit code to this address.
 *
 * Answers 202 whether or not the address has an account here — the reply is
 * deliberately not an existence oracle, so there is nothing in it to branch on.
 */
export function sendCodeRequest(email: string): Promise<{ status: string }> {
  return apiFetch<{ status: string }>("/auth/code", {
    method: "POST",
    body: JSON.stringify({ email }),
  });
}

export function signInWithCodeRequest(
  email: string,
  code: string,
): Promise<AuthResponse> {
  return apiFetch<AuthResponse>("/auth/session", {
    method: "POST",
    body: JSON.stringify({ email, code }),
    // A 401 here means the typed code was wrong, not that a session died —
    // there is no session yet. Same reasoning as changePasswordRequest, and it
    // also keeps this call clear of the refresh-and-retry path in client.ts.
    skipAuthEvent: true,
  });
}

export function completeCallbackRequest(code: string): Promise<AuthResponse> {
  return apiFetch<AuthResponse>("/auth/callback", {
    method: "POST",
    body: JSON.stringify({ code }),
    // A 401 here is a rejected authorization code, not a dead session — there
    // is no session yet. Same reasoning as signInWithCodeRequest.
    skipAuthEvent: true,
  });
}

export function refreshSessionRequest(
  refreshToken: string,
): Promise<AuthResponse> {
  return apiFetch<AuthResponse>("/auth/refresh", {
    method: "POST",
    body: JSON.stringify({ refresh_token: refreshToken }),
    // The refresher is implemented in terms of this call; without the opt-out
    // a 401 here would re-enter the refresher and recurse.
    skipAuthEvent: true,
  });
}

export function meRequest(): Promise<UserPublic> {
  return apiFetch<UserPublic>("/me");
}

export function updateHandleRequest(handle: string): Promise<UserPublic> {
  return apiFetch<UserPublic>("/me", {
    method: "PATCH",
    body: JSON.stringify({ handle }),
  });
}

export function changePasswordRequest(
  currentPassword: string,
  newPassword: string,
): Promise<UserPublic> {
  return apiFetch<UserPublic>("/me/password", {
    method: "PATCH",
    body: JSON.stringify({
      current_password: currentPassword,
      new_password: newPassword,
    }),
    // A 401 here means "current password is wrong," the expected case for a
    // re-auth prompt — not a dead session. See client.ts's ApiFetchInit.
    skipAuthEvent: true,
  });
}

export function setAutoRedeemRequest(enabled: boolean): Promise<UserPublic> {
  return apiFetch<UserPublic>("/me/auto-redeem", {
    method: "PATCH",
    body: JSON.stringify({ enabled }),
  });
}
