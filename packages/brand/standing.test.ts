import { expect, test } from "bun:test";
import { isActive, rankLabel } from "./standing";

test("the top three get a medal, the rest a place, the unranked their progress", () => {
  expect(rankLabel(1, 19, 50)).toEqual({ kind: "medal", place: 1, text: "#1 of 19" });
  expect(rankLabel(3, 19, 50)).toEqual({ kind: "medal", place: 3, text: "#3 of 19" });
  expect(rankLabel(4, 19, 50)).toEqual({ kind: "ranked", text: "#4 of 19" });
  expect(rankLabel(null, 19, 2)).toEqual({ kind: "warming", text: "2 of 3" });
});

test("an agent is active for half an hour after its last trade", () => {
  const now = 1_790_000_000;
  expect([now - 1799, now - 1800, null].map((at) => isActive(at, now))).toEqual([true, false, false]);
});
