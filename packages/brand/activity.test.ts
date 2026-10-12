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
  tx: null,
  eventTitle: null,
  endDate: null,
  resolvedAt: null,
  winner: null,
  kickoff: null, trading: null,
  ...change,
});

test("fills within the hour of a burst's newest fill collapse into it and stay listed", () => {
  const fills = [fill(10_000), fill(9_000, { shares: 50, dollars: 1 }), fill(6_401)];
  const [burst, ...rest] = bursts(fills);
  expect(rest).toEqual([]);
  expect(burst).toEqual({ ...fill(10_000), shares: 250, dollars: 5, fills });
});

test("a burst breaks at the hour", () => {
  expect(bursts([fill(10_000), fill(9_000), fill(6_400)]).map((burst) => burst.fills.length)).toEqual([2, 1]);
});

test("a burst breaks on a different type, side, outcome or title", () => {
  for (const change of [{ type: "REDEEM", side: null }, { side: "SELL" }, { outcome: "No" }, { title: bitcoin }] satisfies Partial<Fill>[]) {
    expect(bursts([fill(10_000), fill(9_999, change), fill(9_998)]).map((burst) => burst.fills.length)).toEqual([1, 1, 1]);
  }
});

test("a fill with a transaction never joins a burst", () => {
  const settled = fill(9_999, { tx: "0x32bd" });
  expect(bursts([fill(10_000), settled, fill(9_998)]).map((burst) => burst.fills)).toEqual([[fill(10_000)], [settled], [fill(9_998)]]);
  expect(bursts([settled, fill(9_998)]).map((burst) => burst.tx)).toEqual(["0x32bd", null]);
});
