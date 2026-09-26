import { expect, test } from "bun:test";
import { createHash } from "node:crypto";
import { robot } from "./robot";

const digest = (address: string) => createHash("sha256").update(robot(address) ?? "").digest("hex");

test("an address always draws the same robot", () => {
  expect(digest("0x998B308bE3F0374bdeE3Ac0Be90198F4bB052958")).toBe("92b662ba518940d496342a7bfca8aa07ff1e55fc369e28b0d28449100dffb278");
  expect(digest("0x91A471C9B99fc0Cd41C89E84acF9A8Ea1EEe6efa")).toBe("65a7645f1de3c07295e65163a823fac5af0c2de08dd4652d8031f4e8bf5daf5e");
});

test("anything but an address draws nothing", () => {
  for (const input of ["", "0x123", "0xZZ8B308bE3F0374bdeE3Ac0Be90198F4bB052958"]) expect(robot(input)).toBeUndefined();
});
