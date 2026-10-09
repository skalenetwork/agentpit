import { expect, test } from "bun:test";
import { cents, compactDollars, etDay, pct, rankMove, shortAddress, shortDay, tone, until, utcTime, zoneTime } from "./format";

test("cents keep one decimal under 10¢ only", () => {
  expect([0, 0.015, 0.044, 0.0999, 0.1, 0.2092, 1].map(cents)).toEqual(["0¢", "1.5¢", "4.4¢", "10¢", "10¢", "21¢", "100¢"]);
});

test("days and times read in UTC", () => {
  const at = new Date(Date.UTC(2026, 8, 24, 23, 8, 59));
  expect(shortDay(at)).toBe("Sep 24");
  expect(utcTime(at)).toBe("23:08 UTC");
});

test("a kickoff reads in its league's time zone", () => {
  const at = new Date(Date.UTC(2026, 9, 4, 0, 30));
  expect([shortDay(at, "America/New_York"), zoneTime(at, "America/New_York"), zoneTime(new Date(Date.UTC(2026, 11, 6, 18)), "America/New_York")]).toEqual(["Oct 3", "20:30 EDT", "13:00 EST"]);
  expect([shortDay(at, "UTC"), zoneTime(at, "UTC")]).toEqual(["Oct 4", "00:30 UTC"]);
});

test("an address shortens to its ends", () => {
  expect(shortAddress("0xEad3078A55752E800Cbc453b7D28aFFB2BB9C15D")).toBe("0xEad3…C15D");
});

test("tone rounds to the cent before it picks a side", () => {
  expect([0.006, 0.004, 0, -0.004, -0.006].map(tone)).toEqual(["up", "flat", "flat", "flat", "down"]);
});

test("a return reads with two decimals, an explicit sign and a true minus", () => {
  expect([12.5, -1.234, 0, -0.001].map(pct)).toEqual(["+12.50%", "−1.23%", "0.00%", "0.00%"]);
});

test("compact dollars keep one decimal and a true minus", () => {
  expect([950, 1234, -1234, 1_250_000].map(compactDollars)).toEqual(["$950", "$1.2K", "−$1.2K", "$1.3M"]);
});

test("a rank move carries the direction and size, and nothing for no change", () => {
  expect([3, -2, 0, null].map(rankMove)).toEqual([{ dir: "up", by: 3 }, { dir: "down", by: 2 }, null, null]);
});

test("a countdown ticks seconds under 5 minutes, then minutes, hours and days", () => {
  expect([0.2, 61, 299, 300, 3_599, 3_600, 18_720, 86_400, 194_400].map(until)).toEqual(["0:01", "1:01", "4:59", "5m", "59m", "1h", "5h 12m", "1d", "2d 6h"]);
});

test("a close date reads in ET, with the year only when it is not this year", () => {
  const now = Date.UTC(2026, 9, 8) / 1000;
  expect([etDay(Date.UTC(2027, 0, 1, 4, 59) / 1000, now), etDay(Date.UTC(2026, 11, 31, 12) / 1000, now)]).toEqual(["Dec 31", "Dec 31"]);
  expect(etDay(Date.UTC(2028, 10, 8, 4, 59) / 1000, now)).toBe("Nov 7, 2028");
});
