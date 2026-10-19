/** The backend's reason from a `{"detail": "<text>"}` body as a sentence, or
 *  `fallback` when the body carries no usable one. */
export function reasonOr(body: string | undefined, fallback: string): string {
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
