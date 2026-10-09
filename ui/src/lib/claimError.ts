const FALLBACK = "Failed to claim.";

/**
 * Maps a failed `POST /positions/claim` response to user-facing copy.
 *
 * The platform pays a claim's gas (`UserGasSponsor`), so there is nothing for
 * the user to fund; the status alone tells the reasons a claim did not happen
 * apart:
 *
 * - 400 is the claim refusing on its own terms ("nothing to claim", "market is
 *   not resolved on chain yet"). The backend's detail says it in words a
 *   person can act on, so it is shown, sentence-cased like every other toast.
 * - 409 is a claim refused so as not to run twice: the account's transaction
 *   lock is held, or an earlier transaction on this market is unconfirmed.
 * - 503 is our side: gas wallet low, top-up not landing in time, or the claim
 *   sent but unconfirmed, where the user must not repeat it. Those need
 *   different sentences, so the backend's own is shown, as for a 400.
 * - 402 is the wallet still unable to pay after the server's top-up and one
 *   retry, or sponsorship switched off. Nothing the user can fix, so the copy
 *   says only that claiming is unavailable.
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
