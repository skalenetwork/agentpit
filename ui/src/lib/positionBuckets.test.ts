import { describe, expect, it } from "vitest";
import {
  closedResult,
  effectivePositionFilter,
  positionBucket,
  unclaimedTotal,
} from "./positionBuckets";

describe("positionBucket", () => {
  it("puts a won-but-unclaimed position in its own bucket", () => {
    expect(positionBucket({ redeemable: true, settled: true })).toBe(
      "unclaimed",
    );
  });

  // The production bug: a market that resolved against the account leaves
  // tokens nobody redeems, so /positions keeps returning it while
  // /closed-positions (built from REDEEM rows) never can. It belongs under
  // Closed, not under Active at a price of zero.
  it("puts a settled position with nothing to claim under closed", () => {
    expect(positionBucket({ redeemable: false, settled: true })).toBe("closed");
  });

  // Price is not the signal, so the bucket cannot read one: a live market can
  // trade at a cent and a resolved winner sits at a dollar. Only `settled`
  // moves a row out of Active.
  it("leaves an unsettled position active whether or not it is redeemable", () => {
    expect(positionBucket({ redeemable: false, settled: false })).toBe(
      "active",
    );
    expect(positionBucket({ redeemable: true, settled: false })).toBe("active");
  });
});

describe("unclaimedTotal", () => {
  it("adds up only what can be claimed", () => {
    expect(
      unclaimedTotal([
        { redeemable: true, currentValue: 100 },
        { redeemable: true, currentValue: 40.5 },
        { redeemable: false, currentValue: 999 },
      ]),
    ).toBe(140.5);
  });

  // A settled loser now has a tab of its own, but it is still not money the
  // account can collect, so the Unclaimed stat must not grow by it.
  it("ignores a position that settled with nothing to claim", () => {
    expect(
      unclaimedTotal([
        { redeemable: true, currentValue: 40.5 },
        { redeemable: false, currentValue: 100 },
      ]),
    ).toBe(40.5);
  });

  it("is zero when there is nothing to claim", () => {
    expect(unclaimedTotal([{ redeemable: false, currentValue: 999 }])).toBe(0);
  });

  it("is zero for an empty list", () => {
    expect(unclaimedTotal([])).toBe(0);
  });
});

describe("effectivePositionFilter", () => {
  it("falls back to active when unclaimed is selected but there is nothing left to claim", () => {
    expect(effectivePositionFilter("unclaimed", 0)).toBe("active");
  });

  it("stays on unclaimed while there is still something to claim", () => {
    expect(effectivePositionFilter("unclaimed", 40.5)).toBe("unclaimed");
  });

  it("leaves active alone regardless of the unclaimed total", () => {
    expect(effectivePositionFilter("active", 0)).toBe("active");
    expect(effectivePositionFilter("active", 100)).toBe("active");
  });

  it("leaves closed alone regardless of the unclaimed total", () => {
    expect(effectivePositionFilter("closed", 0)).toBe("closed");
    expect(effectivePositionFilter("closed", 100)).toBe("closed");
  });
});

describe("closedResult", () => {
  it("calls a profitable exit a win", () => {
    expect(closedResult({ cashPnl: 4.06 })).toBe("won");
  });

  it("calls a losing exit a loss even though money came back", () => {
    // The regression: a position closed by selling reports its proceeds, so
    // the old test (currentValue > 0) was true of every sale. Production had
    // a -$600 exit badged "Won".
    expect(closedResult({ cashPnl: -600 })).toBe("lost");
    expect(closedResult({ cashPnl: -45.66 })).toBe("lost");
  });

  it("treats breaking even as a win rather than a loss", () => {
    // Nothing was lost, and "Lost" beside a $0.00 P/L reads as an error.
    expect(closedResult({ cashPnl: 0 })).toBe("won");
  });
});
