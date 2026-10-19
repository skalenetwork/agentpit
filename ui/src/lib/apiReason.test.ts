import { describe, expect, it } from "vitest";
import { reasonOr } from "./apiReason";

describe("reasonOr", () => {
  it("reads the backend's detail as a sentence", () => {
    expect(reasonOr('{"detail": "at most 10 agents per account"}', "Fallback.")).toBe(
      "At most 10 agents per account.",
    );
  });

  it("falls back when the body has no usable detail", () => {
    expect(reasonOr(undefined, "Fallback.")).toBe("Fallback.");
    expect(reasonOr("not json", "Fallback.")).toBe("Fallback.");
    expect(reasonOr('{"detail": [{"msg": "x"}]}', "Fallback.")).toBe("Fallback.");
    expect(reasonOr('{"detail": "  "}', "Fallback.")).toBe("Fallback.");
  });
});
