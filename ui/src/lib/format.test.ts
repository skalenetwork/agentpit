import { describe, expect, it } from "vitest";
import {
  closeLabel,
  displayTagLabel,
  formatCredits,
  formatCreditsExact,
  formatPnlPct,
  formatProbabilityPct,
  formatShares,
  formatShortDate,
  formatSignedUsd,
  parseVolume,
  relativeTime,
  shortAddress,
  volumeStat,
} from "./format";

describe("parseVolume", () => {
  it("returns null for absent, zero, or unparseable input", () => {
    expect(parseVolume(null)).toBeNull();
    expect(parseVolume(undefined)).toBeNull();
    expect(parseVolume("")).toBeNull();
    expect(parseVolume("0")).toBeNull();
    expect(parseVolume("nope")).toBeNull();
  });

  it("parses a positive numeric string", () => {
    expect(parseVolume("14646954.7")).toBeCloseTo(14646954.7);
    expect(parseVolume("850")).toBe(850);
  });
});

describe("formatProbabilityPct", () => {
  it("renders an em dash for a null mid", () => {
    expect(formatProbabilityPct(null)).toBe("—");
  });

  it("shows '<1' for a non-zero probability under 1%", () => {
    // 0.9¢ used to round down to a misleading "0".
    expect(formatProbabilityPct(0.009)).toBe("<1");
    expect(formatProbabilityPct(0.001)).toBe("<1");
    expect(formatProbabilityPct(0.005)).toBe("<1");
  });

  it("shows '0' only for an exactly-zero (or empty) probability", () => {
    expect(formatProbabilityPct(0)).toBe("0");
  });

  it("rounds 1% and above to the nearest whole percent", () => {
    expect(formatProbabilityPct(0.01)).toBe("1");
    expect(formatProbabilityPct(0.184)).toBe("18");
    expect(formatProbabilityPct(0.995)).toBe("100");
  });
});

describe("formatSignedUsd", () => {
  it("prefixes a positive amount with +", () => {
    expect(formatSignedUsd(84.2)).toBe("+$84.20");
  });

  it("prefixes a negative amount with -", () => {
    expect(formatSignedUsd(-210.8)).toBe("-$210.80");
  });

  it("renders zero without a sign", () => {
    expect(formatSignedUsd(0)).toBe("$0.00");
  });
});

describe("relativeTime", () => {
  it("renders seconds under a minute", () => {
    expect(relativeTime(42)).toBe("42s");
  });

  it("renders whole minutes under an hour", () => {
    expect(relativeTime(5 * 60 + 30)).toBe("5m");
  });

  it("renders whole hours under a day", () => {
    expect(relativeTime(2 * 3600 + 59 * 60)).toBe("2h");
  });

  it("renders whole days from 24h up", () => {
    expect(relativeTime(3 * 86_400 + 5)).toBe("3d");
  });

  it("clamps negative input (clock skew) to 0s", () => {
    expect(relativeTime(-7)).toBe("0s");
  });
});

describe("volumeStat", () => {
  it("shows the 24h figure when the list is ranked by it", () => {
    // Under "24hr Volume" the all-time number contradicts the order the card
    // sits in: $16.2M above $12.0M above $1.4M, in a list sorted by the day.
    expect(volumeStat(16_234_630, 1_566_477, "24h")).toEqual({
      value: 1_566_477,
      label: "24h vol",
    });
  });

  it("falls back to all-time when a 24h figure is asked for and missing", () => {
    expect(volumeStat(8_068_246, null, "24h")).toEqual({
      value: 8_068_246,
      label: "vol",
    });
  });

  it("prefers the all-time figure", () => {
    expect(volumeStat(8_068_246, 3_341_364)).toEqual({
      value: 8_068_246,
      label: "vol",
    });
  });

  it("falls back to 24h, labelled as 24h, when all-time is missing", () => {
    // 545 of 962 production events had exactly this shape: they had dropped out
    // of the synced top-N, so only the older 24h capture survived. Dropping the
    // line for them would have blanked most of the grid.
    expect(volumeStat(null, 3_341_364)).toEqual({
      value: 3_341_364,
      label: "24h vol",
    });
  });

  it("is null only when neither figure exists", () => {
    expect(volumeStat(null, null)).toBeNull();
  });

  it("treats a real zero as a figure, not as missing", () => {
    expect(volumeStat(0, 500)).toEqual({ value: 0, label: "vol" });
  });
});

describe("formatPnlPct", () => {
  it("keeps one decimal so the percent agrees with the dollars beside it", () => {
    // $0.30 on a $9.20 cost. "3%" implied $9.48 against a real $9.50.
    expect(formatPnlPct(3.2608695652173796)).toBe("3.3");
  });

  it("drops a trailing .0 from a clean value", () => {
    expect(formatPnlPct(25)).toBe("25");
    expect(formatPnlPct(-20)).toBe("-20");
  });

  it("preserves the sign", () => {
    expect(formatPnlPct(-21.875)).toBe("-21.9");
  });

  it("rounds half away from zero at the tenth", () => {
    expect(formatPnlPct(6.25)).toBe("6.3");
  });

  it("returns 0 for a non-finite input", () => {
    // cashPnl / 0 cost is Infinity; the row must still render.
    expect(formatPnlPct(Number.POSITIVE_INFINITY)).toBe("0");
    expect(formatPnlPct(Number.NaN)).toBe("0");
  });
});

describe("formatCredits", () => {
  it("renders a whole native-coin amount with four decimals", () => {
    expect(formatCredits("1000000000000000000")).toBe("1.0000");
  });

  it("renders zero", () => {
    expect(formatCredits("0")).toBe("0.0000");
  });

  it("keeps four decimals for a fractional amount", () => {
    expect(formatCredits("1500000000000000000")).toBe("1.5000");
  });

  it("rounds half up at the fourth decimal", () => {
    // 0.00005 native rounds to 0.0001, not down to 0.0000.
    expect(formatCredits("50000000000000")).toBe("0.0001");
  });

  it("truncates rather than rounds up when just under the half", () => {
    expect(formatCredits("49999999999999")).toBe("0.0000");
  });

  it("does not lose precision on a value beyond Number's safe integer range", () => {
    // 12345.6789 native units, expressed in wei -- well past 2^53.
    expect(formatCredits("12345678900000000000000")).toBe("12345.6789");
  });

  it("returns an em dash for input that isn't a valid integer string", () => {
    expect(formatCredits("not-a-number")).toBe("—");
    expect(formatCredits("")).toBe("—");
  });

  it("stays useful at the signup grant's own scale, where two decimals used to read 0.00", () => {
    // 0.02 native signup grant minus the three onboarding approvals
    // (~0.0066 native): a real, still-claimable balance that a two-decimal
    // display flattened to "0.00", indistinguishable from actually empty.
    expect(formatCredits("13400000000000000")).toBe("0.0134");
  });
});

describe("formatCreditsExact", () => {
  it("renders the exact figure with no rounding", () => {
    expect(formatCreditsExact("13400000000000000")).toBe("0.0134");
  });

  it("trims trailing zeros without rounding away real precision", () => {
    expect(formatCreditsExact("1000000000000000000")).toBe("1");
    expect(formatCreditsExact("1230000000000000000")).toBe("1.23");
  });

  it("shows precision finer than the fixed four-decimal headline figure", () => {
    // formatCredits rounds this to "0.0000"; the exact tooltip must not.
    expect(formatCreditsExact("49999999999999")).toBe("0.000049999999999999");
  });

  it("renders zero", () => {
    expect(formatCreditsExact("0")).toBe("0");
  });

  it("returns an em dash for input that isn't a valid integer string", () => {
    expect(formatCreditsExact("not-a-number")).toBe("—");
    expect(formatCreditsExact("")).toBe("—");
  });
});

describe("shortAddress", () => {
  it("keeps both ends so the account stays recognisable", () => {
    expect(shortAddress("0x933B442e9A78e3C3a567B86ee595Eb9BcEb15215")).toBe(
      "0x933B…5215",
    );
  });

  it("preserves case — addresses are checksummed, and lowercasing loses that", () => {
    expect(shortAddress("0xAbCdEf1234567890aaaa")).toBe("0xAbCd…aaaa");
  });

  it("passes through anything too short to be worth truncating", () => {
    expect(shortAddress("0x1234")).toBe("0x1234");
    expect(shortAddress("")).toBe("");
  });
});

describe("displayTagLabel", () => {
  it("title-cases a label upstream left entirely lowercase", () => {
    expect(displayTagLabel("league of legends")).toBe("League of Legends");
    expect(displayTagLabel("primary elections")).toBe("Primary Elections");
    expect(displayTagLabel("baseball")).toBe("Baseball");
    expect(displayTagLabel("counter strike 2")).toBe("Counter Strike 2");
  });

  it("leaves small words lowercase unless they lead", () => {
    expect(displayTagLabel("of mice and men")).toBe("Of Mice and Men");
  });

  it("does not touch a label that already carries a capital", () => {
    // These are the majority, and re-casing them would produce "Ai", "Atp",
    // "Us Election" — worse than the problem being fixed.
    for (const label of [
      "AI",
      "ATP Tour",
      "A100",
      "US Election",
      "Argentina Primera División",
      "U.S. x Iran",
      "Nov 4 Elections",
    ]) {
      expect(displayTagLabel(label)).toBe(label);
    }
  });

  it("handles an empty label without throwing", () => {
    expect(displayTagLabel("")).toBe("");
  });
});

describe("displayTagLabel acronyms", () => {
  it("uppercases the acronyms title-casing would mangle", () => {
    // Observed arriving lowercase from upstream; "Fomc" and "Lol" read as
    // typos beside their correctly-cased neighbours.
    expect(displayTagLabel("fomc")).toBe("FOMC");
    expect(displayTagLabel("lol")).toBe("LoL");
    expect(displayTagLabel("val")).toBe("VAL");
  });

  it("still title-cases ordinary lowercase labels", () => {
    expect(displayTagLabel("baseball")).toBe("Baseball");
    expect(displayTagLabel("league of legends")).toBe("League of Legends");
  });

  it("leaves an already-cased label alone even if it is in the map", () => {
    // Upstream mostly sends these correctly; the map is only for the strays.
    expect(displayTagLabel("MLB")).toBe("MLB");
    expect(displayTagLabel("AI")).toBe("AI");
  });
});

describe("closeLabel", () => {
  const JUN_1 = Math.floor(Date.UTC(2026, 5, 1) / 1000);
  const AUG_10 = Math.floor(Date.UTC(2026, 7, 10) / 1000);
  const DEC_1 = Math.floor(Date.UTC(2026, 11, 1) / 1000);

  it("prints the date while it is still ahead", () => {
    expect(closeLabel(DEC_1, "ACTIVE", AUG_10)).toEqual({
      prefix: "closes",
      value: formatShortDate(DEC_1),
    });
  });

  it("says the deadline lapsed, not that the market is settled", () => {
    // The Ethiopia case: deadline 1 Jun, upstream still reports
    // acceptingOrders true and $678k of 24h volume in August. Saying
    // "Awaiting resolution" told a reader the position was settled when they
    // could still take one.
    expect(closeLabel(JUN_1, "ACTIVE", AUG_10)).toEqual({
      prefix: "overdue",
      value: "Jun 1",
    });
  });

  it("keeps printing the date for a past-dated market that isn't live", () => {
    // 849 events on production are past-dated and fully resolved. Gating on
    // the date alone would have every one of them claim it awaits resolution.
    // DRAFT is the one other reachable state, alongside the finished ones.
    for (const state of ["RESOLVED", "CANCELLED", "CLOSED", "DRAFT"] as const) {
      expect(closeLabel(JUN_1, state, AUG_10)).toEqual({
        prefix: "closes",
        value: "Jun 1",
      });
    }
  });

  it("has nothing to say without a date", () => {
    expect(closeLabel(null, "ACTIVE", AUG_10)).toBeNull();
  });

  it("uses a caller-provided formatter instead of the default short date", () => {
    // The detail pages render long dates ("Jun 1, 2026"); this is how they
    // share the overdue-and-live rule without duplicating it.
    const longFormat = (s: number) => `LONG:${s}`;
    expect(closeLabel(DEC_1, "ACTIVE", AUG_10, longFormat)).toEqual({
      prefix: "closes",
      value: `LONG:${DEC_1}`,
    });
  });

  it("uses the caller-provided formatter for the overdue date too", () => {
    // Detail pages render "overdue Jun 1, 2026", not the short-date "Jun 1" —
    // the overdue branch shares the same formatter as the on-time one.
    const longFormat = (s: number) => `LONG:${s}`;
    expect(closeLabel(JUN_1, "ACTIVE", AUG_10, longFormat)).toEqual({
      prefix: "overdue",
      value: `LONG:${JUN_1}`,
    });
  });
});

describe("formatShares", () => {
  it("cuts a market order's division down to something readable", () => {
    // $1 at a 71c cap is 1.4084507042253522 shares, and the trade button was
    // printing all sixteen digits.
    expect(formatShares(1.4084507042253522)).toBe("1.41");
  });

  it("leaves a whole share count without decimals", () => {
    expect(formatShares(12)).toBe("12");
  });

  it("groups thousands", () => {
    expect(formatShares(1234.5)).toBe("1,234.5");
  });

  it("keeps zero as zero", () => {
    expect(formatShares(0)).toBe("0");
  });
});
