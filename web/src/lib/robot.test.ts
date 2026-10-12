import { expect, test } from "bun:test";
import { createHash } from "node:crypto";
import { person, robot } from "./robot";

const digest = (address: string) => createHash("sha256").update(robot(address) ?? "").digest("hex");

test("an address always draws the same robot", () => {
  expect(digest("0x998B308bE3F0374bdeE3Ac0Be90198F4bB052958")).toBe("92b662ba518940d496342a7bfca8aa07ff1e55fc369e28b0d28449100dffb278");
  expect(digest("0x91A471C9B99fc0Cd41C89E84acF9A8Ea1EEe6efa")).toBe("65a7645f1de3c07295e65163a823fac5af0c2de08dd4652d8031f4e8bf5daf5e");
});

test("an owner name draws one person in any letter case", () => {
  const svg = person("CopperCove");
  expect(svg).toStartWith("<svg");
  expect(person("coppercove")).toBe(svg);
});

test("anything but an address draws no robot", () => {
  for (const input of ["", "0x123", "0xZZ8B308bE3F0374bdeE3Ac0Be90198F4bB052958"]) expect(robot(input)).toBeUndefined();
});

test("anything but an owner name draws no person", () => {
  for (const input of ["", "copper-cove", "SixteenCharsLong", "0x998B308bE3F0374bdeE3Ac0Be90198F4bB052958"]) expect(person(input)).toBeUndefined();
});
