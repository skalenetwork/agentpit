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

  it("409: another transaction holds the account's lock", () => {
    expect(
      claimErrorMessage(
        409,
        detail(
          "another transaction for this account is in progress — try again in a moment",
        ),
      ),
    ).toBe("A claim is already in progress.");
  });

  it("503: the platform's gas wallet is low", () => {
    expect(
      claimErrorMessage(
        503,
        detail("the platform's gas wallet is running low — try again later"),
      ),
    ).toBe("Claims are paused, try again later.");
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
