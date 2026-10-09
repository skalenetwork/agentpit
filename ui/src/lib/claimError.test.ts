import { describe, expect, it } from "vitest";
import { claimErrorMessage } from "./claimError";

/** The body FastAPI sends for every domain error: `{"detail": "<text>"}`. */
const detail = (text: unknown) => JSON.stringify({ detail: text });

describe("claimErrorMessage", () => {
  it("400: shows the backend's reason as a sentence", () => {
    expect(claimErrorMessage(400, detail("nothing to claim"))).toBe(
      "Nothing to claim.",
    );
    expect(
      claimErrorMessage(400, detail("market is not resolved on chain yet")),
    ).toBe("Market is not resolved on chain yet.");
  });

  it("400: does not double the backend's own closing punctuation", () => {
    expect(claimErrorMessage(400, detail("the claim reverted on chain."))).toBe(
      "The claim reverted on chain.",
    );
  });

  it("400 without a sentence to show falls back to the generic message", () => {
    expect(claimErrorMessage(400, "")).toBe("Failed to claim.");
    expect(claimErrorMessage(400, undefined)).toBe("Failed to claim.");
    // A proxy's error page, not FastAPI.
    expect(claimErrorMessage(400, "<html>Bad Request</html>")).toBe(
      "Failed to claim.",
    );
    expect(claimErrorMessage(400, detail("   "))).toBe("Failed to claim.");
    // A validation-style array is not a sentence either.
    expect(claimErrorMessage(400, detail([{ msg: "field required" }]))).toBe(
      "Failed to claim.",
    );
  });

  it("409: shows the backend's reason, a held lock or an unconfirmed transaction", () => {
    expect(
      claimErrorMessage(
        409,
        detail(
          "another transaction for this account is in progress — try again in a moment",
        ),
      ),
    ).toBe(
      "Another transaction for this account is in progress — try again in a moment.",
    );
    expect(
      claimErrorMessage(
        409,
        detail("an earlier transaction on this market is not confirmed yet"),
      ),
    ).toBe("An earlier transaction on this market is not confirmed yet.");
  });

  it("503: shows the backend's reason, paused, busy or sent but unconfirmed", () => {
    expect(
      claimErrorMessage(
        503,
        detail("the platform's gas wallet is running low — try again later"),
      ),
    ).toBe("The platform's gas wallet is running low — try again later.");
    expect(
      claimErrorMessage(
        503,
        detail("the platform is busy — try again in a moment"),
      ),
    ).toBe("The platform is busy — try again in a moment.");
    // A claim that was sent but not confirmed: the user must not repeat it,
    // so "try again later" would be the wrong thing to say.
    const pending = claimErrorMessage(
      503,
      detail(
        "the transaction was sent but is not confirmed yet — it will appear in your history once it lands; do not repeat it",
      ),
    );
    expect(pending).toBe(
      "The transaction was sent but is not confirmed yet — it will appear in your history once it lands; do not repeat it.",
    );
    expect(pending).not.toMatch(/try again/i);
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

  it("falls back to the generic message for anything else", () => {
    expect(claimErrorMessage(404, detail("Market not found"))).toBe(
      "Failed to claim.",
    );
    expect(claimErrorMessage(500, "Internal Server Error")).toBe(
      "Failed to claim.",
    );
    expect(claimErrorMessage(undefined, undefined)).toBe("Failed to claim.");
  });
});
