import { expect, test } from "bun:test";
import { runnerLabel, signedPercent, tone, when } from "./labels";

test("a return carries its sign and a true minus", () => {
  expect([10.816, -4.38, 0].map(signedPercent)).toEqual(["+10.82%", "−4.38%", "0.00%"]);
});

test("only a real gain or loss is coloured", () => {
  expect([0.52, -18.13, 0.004, 0].map(tone)).toEqual(["text-up", "text-down", "text-ink", "text-ink"]);
});

test("a time within a day is relative, older is a date", () => {
  const now = Date.UTC(2026, 9, 1, 11, 0) / 1000;
  expect([when(now - 13 * 3600, now), when(now - 3 * 86400, now)]).toEqual(["13h ago", "Sep 28"]);
});

test("a runner names its host only when it has one", () => {
  expect(runnerLabel({ slug: "codex", label: "Codex", host: "Self-hosted" })).toBe("Codex, self-hosted");
  expect(runnerLabel({ slug: "api", label: "API", host: null })).toBe("API");
});
