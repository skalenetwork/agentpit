import { expect, test } from "bun:test";
import type { Race, Standing } from "./api";
import { raceChart, raceTicks, sparkline, trendChart } from "./chart";

test("the trend spans the box with zero inside it and takes the tone of the last close", () => {
  expect(trendChart([0, 100, -100])).toEqual({
    d: "M0,500L500,60L1000,940",
    zero: 500,
    tone: "down",
  });
});

test("a single close sits at the right edge, above a zero at the bottom", () => {
  expect(trendChart([5])).toEqual({ d: "M1000,60", zero: 940, tone: "up" });
  expect(trendChart([0.001]).tone).toBe("flat");
});

test("a sparkline ends at the right edge of a 30-day axis and needs two values", () => {
  expect(sparkline([1, 2], 30)).toEqual({ points: "89.9,25 93,3", end: { x: 93, y: 3 } });
  expect(sparkline([...Array(30).keys()], 30)?.points.startsWith("3,25 ")).toBe(true);
  expect(sparkline([1], 30)).toBeNull();
  expect(sparkline([], 30)).toBeNull();
});

const DAY = 86_400;
const NOV = Date.UTC(2026, 10, 1) / 1000;

const standing = (address: string, name: string, rank: number | null): Standing => ({
  address,
  name,
  runner: { slug: "api", label: "API", host: null },
  listed: true,
  runStartsAt: NOV,
  endAt: null,
  trades: 3,
  lastTradeAt: null,
  disqualified: false,
  rank,
  pnl: 0,
  returnPct: 0,
  rankChange: null,
  behind: null,
  wouldRank: null,
  hoursLed: 0,
  bids: null,
  bell: null,
});

const live: Race = {
  slug: "monthly",
  title: "Monthly",
  startsAt: NOV,
  endsAt: NOV + 10 * DAY,
  phase: "live",
  final: false,
  prizes: [],
  entrants: 3,
  ranked: 3,
  line: "",
  builtAt: NOV + 5 * DAY,
  leadChanges: 1,
  fixtures: [],
  tape: [],
  standings: [standing("0xA", "Alpha", 1), standing("0xB", "Bravo", 2), standing("0xC", "Charlie", 3)],
  series: {
    at: [1, 2, 3, 4].map((k) => NOV + k * DAY),
    lines: [
      { address: "0xA", ranks: [null, 2, 1, 1, 1] },
      { address: "0xC", ranks: [null, null, 3, 3, 3] },
      { address: "0xB", ranks: [1, 1, 2, 2, 2] },
    ],
  },
};

test("race lanes run to now with gaps cut, colours in order of first placing and names right of the robot", () => {
  const chart = raceChart(live, "0xc");
  expect(chart?.levels).toEqual([0, 50, 100]);
  expect(chart?.lanes.map(({ standing, hue, own, d, start, end, bell, side }) => ({ name: standing.name, hue, own, d, start, end, bell, side }))).toEqual([
    { name: "Bravo", hue: 0, own: false, d: "M100,0L200,0L300,1L500,1", start: { x: 10, y: 0 }, end: { x: 50, y: 50, dx: 0 }, bell: null, side: "right" },
    { name: "Alpha", hue: 1, own: false, d: "M200,1L300,0L500,0", start: { x: 20, y: 50 }, end: { x: 50, y: 0, dx: 0 }, bell: null, side: "right" },
    { name: "Charlie", hue: 2, own: true, d: "M300,2L500,2", start: { x: 30, y: 100 }, end: { x: 50, y: 100, dx: 0 }, bell: null, side: "right" },
  ]);
});

test("an agent outside the top ten draws only when asked for, last, ending where it left the chart", () => {
  const crowd: Race = {
    ...live,
    ranked: 12,
    standings: [...live.standings, standing("0xD", "Delta", 12), standing("0xE", "Echo", 11)],
    series: { ...live.series, lines: [...live.series.lines, { address: "0xD", ranks: [12, 4, 11, 12, 12] }, { address: "0xE", ranks: [11, 3, 12, 11, 11] }] },
  };
  expect(raceChart(crowd)?.lanes.map((lane) => lane.standing.name)).toEqual(["Bravo", "Alpha", "Charlie"]);
  const delta = raceChart(crowd, "0xd")?.lanes.at(-1);
  expect([delta?.standing.name, delta?.hue, delta?.own, delta?.d, delta?.end]).toEqual(["Delta", 3, true, "M200,3", { x: 20, y: 33.33, dx: 0 }]);
});

test("ended lines stop at the bell; once final each steps to its final place past the right edge, named on the left", () => {
  const final: Race = {
    ...live,
    phase: "ended",
    final: true,
    ranked: 2,
    builtAt: NOV + 11 * DAY,
    standings: [standing("0xB", "Bravo", 1), standing("0xA", "Alpha", 2)],
    series: {
      at: [NOV + 5 * DAY, NOV + 10 * DAY],
      lines: [
        { address: "0xB", ranks: [2, 2, 1] },
        { address: "0xA", ranks: [1, 1, 2] },
      ],
    },
  };
  expect(raceChart(final)?.lanes.map(({ standing, hue, d, end, bell, side }) => [standing.name, hue, d, end, bell, side])).toEqual([
    ["Alpha", 0, "M500,0L1000,0", { x: 100, y: 100, dx: 32 }, 0, "left"],
    ["Bravo", 1, "M500,1L1000,1", { x: 100, y: 0, dx: 32 }, 100, "left"],
  ]);
  expect(raceChart({ ...final, final: false })?.lanes.map(({ d, end, bell }) => [d, end, bell])).toEqual([
    ["M500,0L1000,0L1000,1", { x: 100, y: 100, dx: 0 }, null],
    ["M500,1L1000,1L1000,0", { x: 100, y: 0, dx: 0 }, null],
  ]);
});

test("a race needs two placed agents and two columns for a chart", () => {
  expect(raceChart({ ...live, ranked: 1 })).toBeNull();
  expect(raceChart({ ...live, series: { ...live.series, at: [NOV + DAY] } })).toBeNull();
});

test("a month ticks weekly, or fortnightly on a phone, with Now in place of the ticks it crowds", () => {
  const month: Race = { ...live, endsAt: NOV + 30 * DAY, builtAt: NOV + 12.5 * DAY };
  const tick = (x: number, label: string, anchor = "middle") => ({ x, dx: 0, label, anchor, now: label === "Now" });
  expect(raceTicks(month)).toEqual([tick(0, "Nov 1", "start"), tick(23.33, "Nov 8"), tick(70, "Nov 22"), tick(41.67, "Now"), tick(100, "Dec 1", "end")]);
  expect(raceTicks(month, true)).toEqual([tick(0, "Nov 1", "start"), tick(41.67, "Now"), tick(100, "Dec 1", "end")]);
});

test("a final week ticks daily and ends on Final past the edge; Now near the end hides the end date", () => {
  const week: Race = { ...live, endsAt: NOV + 7 * DAY, phase: "ended", final: true };
  expect(raceTicks(week).map(({ label }) => label)).toEqual(["Nov 1", "Nov 2", "Nov 3", "Nov 4", "Nov 5", "Nov 6", "Nov 7", "Final"]);
  expect(raceTicks(week).at(-1)).toEqual({ x: 100, dx: 32, label: "Final", anchor: "middle", now: false });
  expect(raceTicks(week, true).map(({ label }) => label)).toEqual(["Nov 1", "Nov 3", "Nov 5", "Nov 7", "Final"]);
  expect(raceTicks({ ...week, phase: "live", final: false, builtAt: NOV + 6.6 * DAY }).map(({ label }) => label)).toEqual(["Nov 1", "Nov 2", "Nov 3", "Nov 4", "Nov 5", "Nov 6", "Now"]);
});
