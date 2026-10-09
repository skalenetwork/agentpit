import { describe, expect, it } from "vitest";
import { claimErrorMessage } from "./claimError";

/** The body FastAPI sends for every domain error: `{"detail": "<text>"}`. */
const detail = (text: unknown) => JSON.stringify({ detail: text });

const FAILED = "Failed to claim.";
// A claim that was sent but not confirmed: the user must not repeat it.
const PENDING =
  "the transaction was sent but is not confirmed yet — it will appear in your history once it lands; do not repeat it";

describe("claimErrorMessage", () => {
  // The backend's reason, shown as a sentence: capitalised, one closing full stop.
  // prettier-ignore
  it.each([
    [400, "nothing to claim", "Nothing to claim."],
    [400, "market is not resolved on chain yet", "Market is not resolved on chain yet."],
    [400, "the claim reverted on chain.", "The claim reverted on chain."],
    [409, "another transaction for this account is in progress — try again in a moment", "Another transaction for this account is in progress — try again in a moment."],
    [409, "an earlier transaction on this market is not confirmed yet", "An earlier transaction on this market is not confirmed yet."],
    [503, "the platform's gas wallet is running low — try again later", "The platform's gas wallet is running low — try again later."],
    [503, "the platform is busy — try again in a moment", "The platform is busy — try again in a moment."],
    [503, PENDING, "The transaction was sent but is not confirmed yet — it will appear in your history once it lands; do not repeat it."],
  ])("%i: shows the reason %j", (status, reason, shown) => {
    expect(claimErrorMessage(status, detail(reason))).toBe(shown);
  });

  it("503: a sent but unconfirmed claim never says to try again", () => {
    expect(claimErrorMessage(503, detail(PENDING))).not.toMatch(/try again/i);
  });

  it("402: says claiming is unavailable and never asks the user to fund anything", () => {
    const message = claimErrorMessage(
      402,
      detail("wallet balance too low to pay for this transaction's gas"),
    );
    expect(message).toBe("Claiming is unavailable right now.");
    // The old copy sent the user off to fund an address. There is nothing
    // for them to fund any more: the platform pays a claim's gas.
    expect(message).not.toMatch(/credits|send|0x/i);
  });

  // 400 without a sentence to show, and anything else.
  // prettier-ignore
  const generic: [number | undefined, string | undefined][] = [
    [400, ""],
    [400, undefined],
    [400, "<html>Bad Request</html>"], // a proxy's error page, not FastAPI
    [400, detail("   ")],
    [400, detail([{ msg: "field required" }])], // a validation-style array is no sentence
    [404, detail("Market not found")],
    [500, "Internal Server Error"],
    [undefined, undefined],
  ];
  it.each(generic)("%s with body %j: the generic message", (status, body) => {
    expect(claimErrorMessage(status, body)).toBe(FAILED);
  });
});
