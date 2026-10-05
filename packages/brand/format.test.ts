import { expect, test } from "bun:test";
import { cents, compactDollars, pct, rankMove, shortAddress, shortDay, tone, utcTime } from "./format";

test("cents keep one decimal under 10¢ only", () => {
  expect([0, 0.015, 0.044, 0.0999, 0.1, 0.2092, 1].map(cents)).toEqual(["0¢", "1.5¢", "4.4¢", "10¢", "10¢", "21¢", "100¢"]);
});

test("days and times read in UTC", () => {
  const at = new Date(Date.UTC(2026, 8, 24, 23, 8, 59));
  expect(shortDay(at)).toBe("Sep 24");
  expect(utcTime(at)).toBe("23:08 UTC");
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
