import { expect, test } from "bun:test";
import { cents, percent, shortAddress, shortDay, utcTime } from "./format";

test("cents keep one decimal under 10¢ only", () => {
  expect([0, 0.015, 0.044, 0.0999, 0.1, 0.2092, 1].map(cents)).toEqual(["0¢", "1.5¢", "4.4¢", "10¢", "10¢", "21¢", "100¢"]);
});

test("a share reads as a whole percent", () => {
  expect([0, 0.336, 0.995, 1].map(percent)).toEqual(["0%", "34%", "100%", "100%"]);
});

test("days and times read in UTC", () => {
  const at = new Date(Date.UTC(2026, 8, 24, 23, 8, 59));
  expect(shortDay(at)).toBe("Sep 24");
  expect(utcTime(at)).toBe("23:08 UTC");
});

test("an address shortens to its ends", () => {
  expect(shortAddress("0xEad3078A55752E800Cbc453b7D28aFFB2BB9C15D")).toBe("0xEad3…C15D");
});
