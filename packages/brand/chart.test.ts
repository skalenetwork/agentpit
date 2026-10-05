import { expect, test } from "bun:test";
import { closeLabel, recordBars, sparkline, trendChart } from "./chart";

test("the trend spans the box with zero inside it and takes the tone of the last close", () => {
  expect(trendChart([0, 100, -100])).toEqual({
    d: "M0,500L500,60L1000,940",
    zero: 500,
    points: [
      { x: 0, y: 500 },
      { x: 500, y: 60 },
      { x: 1000, y: 940 },
    ],
    tone: "down",
  });
});

test("a single close sits at the right edge, above a zero at the bottom", () => {
  expect(trendChart([5])).toEqual({ d: "M1000,60", zero: 940, points: [{ x: 1000, y: 60 }], tone: "up" });
  expect(trendChart([0.001]).tone).toBe("flat");
});

test("a sparkline ends at the right edge of a 30-day axis and needs two values", () => {
  expect(sparkline([1, 2], 30)).toEqual({ points: "89.9,25 93,3", end: { x: 93, y: 3 } });
  expect(sparkline([...Array(30).keys()], 30)?.points.startsWith("3,25 ")).toBe(true);
  expect(sparkline([1], 30)).toBeNull();
  expect(sparkline([], 30)).toBeNull();
});

test("record bars grow from one zero line, with a floor height and 16 slots at least", () => {
  expect(recordBars([30, -10, 20, -0.5])).toEqual({
    zero: 75,
    width: 4.38,
    bars: [
      { x: 0.94, y: 0, height: 75, tone: "up" },
      { x: 7.19, y: 75, height: 25, tone: "down" },
      { x: 13.44, y: 25, height: 50, tone: "up" },
      { x: 19.69, y: 75, height: 6, tone: "down" },
    ],
  });
  expect(recordBars([0, 0, 0]).zero).toBe(0);
});

test("close labels count days from the trend start and end on Today", () => {
  expect([0, 3, 29].map((index) => closeLabel("2026-09-29", index, 30))).toEqual(["Sep 29", "Oct 2", "Today"]);
  expect(closeLabel(null, 0, 2)).toBe("");
  expect(closeLabel(null, 0, 1)).toBe("Today");
});
