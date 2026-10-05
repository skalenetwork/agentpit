import { expect, test } from "bun:test";
import { bursts } from "./activity";
import type { Fill } from "./api";

const nobel = "Will World Central Kitchen win the Nobel Peace Prize in 2026?";
const bitcoin = "Will Bitcoin dip to $40,000 by December 31, 2026?";
const fill = (at: number, change: Partial<Fill> = {}): Fill => ({
  at,
  type: "TRADE",
  side: "BUY",
  outcome: "Yes",
  shares: 100,
  dollars: 2,
  title: nobel,
  icon: null,
  category: "World",
  url: null,
  eventTitle: null,
  endDate: null,
  resolvedAt: null,
  winner: null,
  ...change,
});

test("fills within the hour of a burst's newest fill collapse into it", () => {
  const [burst, ...rest] = bursts([fill(10_000), fill(9_000, { shares: 50, dollars: 1 }), fill(6_401)]);
  expect(rest).toEqual([]);
  expect(burst).toEqual({ ...fill(10_000), n: 3, shares: 250, dollars: 5 });
});

test("a burst breaks at the hour", () => {
  expect(bursts([fill(10_000), fill(9_000), fill(6_400)]).map((burst) => burst.n)).toEqual([2, 1]);
});

test("a burst breaks on a different type, side, outcome or title", () => {
  for (const change of [{ type: "REDEEM", side: null }, { side: "SELL" }, { outcome: "No" }, { title: bitcoin }] satisfies Partial<Fill>[]) {
    expect(bursts([fill(10_000), fill(9_999, change), fill(9_998)]).map((burst) => burst.n)).toEqual([1, 1, 1]);
  }
});
