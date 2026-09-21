import { describe, expect, it } from "vitest";
import { normaliseQuantity } from "./quantity";

describe("normaliseQuantity", () => {
  it("takes a plain number", () => {
    expect(normaliseQuantity("12", "1")).toBe("12");
    expect(normaliseQuantity("1250", "125")).toBe("1250");
  });

  it("refuses letters instead of storing them", () => {
    // The regression: the Shares and Amount fields accepted "вавыа" and only
    // complained when the order was sent.
    expect(normaliseQuantity("вавыа", "12")).toBe("12");
    expect(normaliseQuantity("12a", "12")).toBe("12");
    expect(normaliseQuantity("abc", "")).toBe("");
  });

  it("refuses a negative or an exponent", () => {
    expect(normaliseQuantity("-5", "5")).toBe("5");
    expect(normaliseQuantity("1e6", "1")).toBe("1");
  });

  it("keeps two decimals and no more", () => {
    expect(normaliseQuantity("12.5", "12.")).toBe("12.5");
    expect(normaliseQuantity("12.55", "12.5")).toBe("12.55");
    expect(normaliseQuantity("12.555", "12.55")).toBe("12.55");
  });

  it("lets a decimal be typed", () => {
    // "12." is a real intermediate state, not a malformed number.
    expect(normaliseQuantity("12.", "12")).toBe("12.");
    expect(normaliseQuantity(".5", "")).toBe("0.5");
  });

  it("refuses a second decimal point", () => {
    expect(normaliseQuantity("1.2.3", "1.2")).toBe("1.2");
  });

  it("drops a leading zero that is only padding", () => {
    expect(normaliseQuantity("05", "0")).toBe("5");
    expect(normaliseQuantity("0", "")).toBe("0");
    expect(normaliseQuantity("0.5", "0.")).toBe("0.5");
  });

  it("lets the field be emptied", () => {
    expect(normaliseQuantity("", "12")).toBe("");
  });

  it("takes the comma decimal separator", () => {
    expect(normaliseQuantity("12,5", "12")).toBe("12.5");
  });
});
