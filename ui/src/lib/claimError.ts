const FALLBACK = "Failed to claim.";

/**
 * Maps a failed `POST /positions/claim` response to user-facing copy.
 *
 * The platform pays a claim's gas now: before the transaction the server
 * tops the wallet up to exactly what it needs (`UserGasSponsor`). So there is
 * nothing left for the user to fund and no address to send anything to, and
 * the old toast asking the user to top up their own wallet is gone. What is left
 * are a few different reasons a claim did not happen, and the status alone
 * tells the families apart:
 *
 * - 400 is the claim refusing on its own terms: "nothing to claim" (no
 *   winning tokens, or a payout under the $0.01 minimum) or "market is not
 *   resolved on chain yet". The backend's detail already says it in words a
 *   person can act on, so it is shown, sentence-cased to read like every
 *   other toast in the app.
 * - 409 is a claim refused so as not to run twice: the account's transaction
 *   lock is held (auto-redeem or an earlier click is claiming), or an earlier
 *   transaction on this market is sent but not confirmed yet.
 * - 503 is our side: the platform's gas wallet running low (paused), the
 *   top-up not landing in time (busy, try again), or the claim sent but not
 *   confirmed yet, where the one thing the user must not do is repeat it.
 *   Those three need three different sentences, so the backend's own is
 *   shown, the same way as for a 400.
 * - 402 is the wallet still unable to pay after the server's top-up and one
 *   retry, or sponsorship switched off by the operator. Neither is anything
 *   the user can fix, so the copy says only that claiming is unavailable.
 *
 * A 409 or 503 whose body is not `{"detail": "<text>"}` gets a fixed line for
 * its status; anything else, including a 400 with no usable detail, gets the
 * generic message.
 */
export function claimErrorMessage(
  status: number | undefined,
  body: string | undefined,
): string {
  if (status === 400) return reasonOr(body, FALLBACK);
  if (status === 409) return reasonOr(body, "A claim is already in progress.");
  if (status === 503)
    return reasonOr(body, "Claims are paused, try again later.");
  if (status === 402) return "Claiming is unavailable right now.";
  return FALLBACK;
}

/** The backend's reason as a sentence, or `fallback` when the body carries no
 *  usable one. */
function reasonOr(body: string | undefined, fallback: string): string {
  const detail = backendDetail(body);
  return detail === null ? fallback : asSentence(detail);
}

/** The `detail` string of a FastAPI error body, or null when the body is
 *  empty, is not JSON (a proxy's error page), or carries something other
 *  than a sentence (a validation array). */
function backendDetail(body: string | undefined): string | null {
  if (!body) return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(body);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null || !("detail" in parsed)) {
    return null;
  }
  const { detail } = parsed;
  if (typeof detail !== "string") return null;
  const text = detail.trim();
  return text === "" ? null : text;
}

/** "nothing to claim" -> "Nothing to claim." The backend writes its details
 *  lower-case and unpunctuated, for logs and MCP callers as much as for
 *  people; a toast reads as a sentence. */
function asSentence(text: string): string {
  const capitalised = text.charAt(0).toUpperCase() + text.slice(1);
  return /[.!?]$/.test(capitalised) ? capitalised : `${capitalised}.`;
}
