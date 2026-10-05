import { expect, test } from "bun:test";
import type { Call } from "./api";
import { isActive, rankLabel, recordCalls } from "./standing";

const call = (pnl: number): Call => ({ title: "Will it rain?", icon: null, category: "Weather", outcome: "Yes", entry: 0.4, exit: 1, pnl, url: null, eventTitle: null, endDate: null, resolvedAt: null, winner: null });

test("the top three get a medal, the rest a place, the unranked their progress", () => {
  expect(rankLabel(1, 19, 50)).toEqual({ kind: "medal", place: 1, text: "#1 of 19" });
  expect(rankLabel(3, 19, 50)).toEqual({ kind: "medal", place: 3, text: "#3 of 19" });
  expect(rankLabel(4, 19, 50)).toEqual({ kind: "ranked", text: "#4 of 19" });
  expect(rankLabel(null, 19, 4)).toEqual({ kind: "warming", text: "4 of 10" });
});

test("calls are named by what the record holds", () => {
  const label = (wins: number, losses: number, best: number, worst: number) => recordCalls({ wins, losses, best: call(best), worst: call(worst) }).map((item) => item.label);
  expect(label(1, 0, 20, 20)).toEqual(["Only call"]);
  expect(label(3, 2, 20, -5)).toEqual(["Best call", "Worst call"]);
  expect(label(3, 0, 20, 5)).toEqual(["Biggest win", "Smallest win"]);
  expect(label(0, 3, -2, -9)).toEqual(["Smallest loss", "Biggest loss"]);
  expect(recordCalls({ wins: 1, losses: 1, best: call(20), worst: call(-5) }).map((item) => item.call.pnl)).toEqual([20, -5]);
});

test("an agent is active for half an hour after its last trade", () => {
  const now = 1_790_000_000;
  expect([now - 1799, now - 1800, null].map((at) => isActive(at, now))).toEqual([true, false, false]);
});
