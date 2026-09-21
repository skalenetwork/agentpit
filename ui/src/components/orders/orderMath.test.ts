import { describe, expect, it } from "vitest";
import {
  MAX_PROB,
  MIN_PROB,
  SLIPPAGE_CAP,
  bestAsk,
  bestBid,
  centsToPrice,
  computeMarketBuy,
  computeMarketSell,
  cumulativeTotals,
  deriveNoAsk,
  dollarsFromShares,
  levelsFrom,
  pickSellOutcome,
  sharesFromDollars,
} from "./orderMath";
import type { OrderBookLevel } from "@/types/order";

const mkLevel = (price: number, size = 1): OrderBookLevel => ({
  price: String(price),
  size: String(size),
});

describe("bestAsk", () => {
  it("returns the lowest ask in dollars", () => {
    expect(bestAsk([mkLevel(0.185), mkLevel(0.2)])).toBeCloseTo(0.185, 5);
  });

  it("returns null for an empty book side", () => {
    expect(bestAsk([])).toBeNull();
  });
});

describe("bestBid", () => {
  it("returns the highest bid in dollars", () => {
    expect(bestBid([mkLevel(0.175), mkLevel(0.17)])).toBeCloseTo(0.175, 5);
  });

  it("returns null for an empty book side", () => {
    expect(bestBid([])).toBeNull();
  });
});

describe("deriveNoAsk", () => {
  it("uses the NO book's own best ask when present", () => {
    expect(deriveNoAsk([mkLevel(0.826)], [mkLevel(0.175)])).toBeCloseTo(0.826, 5);
  });

  it("falls back to the complement of the YES best bid when NO is empty", () => {
    // NO ask mirrors a YES bid: 1.0 − 0.175 = 0.825
    expect(deriveNoAsk([], [mkLevel(0.175), mkLevel(0.17)])).toBeCloseTo(0.825, 5);
  });

  it("returns null when neither side is known", () => {
    expect(deriveNoAsk([], [])).toBeNull();
  });
});

describe("cumulativeTotals", () => {
  // Numbers taken verbatim from a live Polymarket book (Spain @ 16%).
  it("accumulates ask notional from the touch (bottom row) upward", () => {
    // display order top→bottom; 16.3¢ (last) is the best ask at the spread.
    const asks = [
      { price: 0.165, size: 124315.47 },
      { price: 0.164, size: 67344.34 },
      { price: 0.163, size: 164674.77 },
    ];
    const totals = cumulativeTotals(asks, "ask");
    expect(totals[2]).toBeCloseTo(26841.99, 1); // touch alone
    expect(totals[1]).toBeCloseTo(37886.46, 1); // + 16.4¢
    expect(totals[0]).toBeCloseTo(58398.51, 1); // + 16.5¢
  });

  it("accumulates bid notional from the touch (top row) downward", () => {
    // display order top→bottom; 16.2¢ (first) is the best bid at the spread.
    const bids = [
      { price: 0.162, size: 514436.58 },
      { price: 0.161, size: 180674.75 },
      { price: 0.16, size: 873480.63 },
    ];
    const totals = cumulativeTotals(bids, "bid");
    expect(totals[0]).toBeCloseTo(83338.73, 1); // touch alone
    expect(totals[1]).toBeCloseTo(112427.36, 1); // + 16.1¢
    expect(totals[2]).toBeCloseTo(252184.26, 1); // + 16.0¢
  });

  it("returns an empty array for an empty side", () => {
    expect(cumulativeTotals([], "ask")).toEqual([]);
  });
});

describe("levelsFrom", () => {
  it("parses decimal strings into numeric dollars and shares", () => {
    const levels = levelsFrom([
      { price: "0.45", size: "100" },
      { price: "0.46", size: "50" },
    ]);
    expect(levels).toHaveLength(2);
    expect(levels[0]?.price).toBeCloseTo(0.45, 5);
    expect(levels[0]?.size).toBe(100);
    expect(levels[1]?.price).toBeCloseTo(0.46, 5);
    expect(levels[1]?.size).toBe(50);
  });
});

describe("sharesFromDollars", () => {
  it("converts $50 at 0.65 to 76.923 shares (rounded down)", () => {
    expect(sharesFromDollars(50, 0.65)).toBeCloseTo(76.923076, 5);
  });

  it("returns 0 when price is 0", () => {
    expect(sharesFromDollars(50, 0)).toBe(0);
  });

  it("returns 0 when amount is 0", () => {
    expect(sharesFromDollars(0, 0.65)).toBe(0);
  });
});

describe("dollarsFromShares", () => {
  it("converts 100 shares at 0.65 to $65", () => {
    expect(dollarsFromShares(100, 0.65)).toBeCloseTo(65, 5);
  });

  it("returns 0 when shares is 0", () => {
    expect(dollarsFromShares(0, 0.65)).toBe(0);
  });

  it("round-trips: dollarsFromShares(sharesFromDollars(amt, p), p) ≈ amt", () => {
    expect(dollarsFromShares(sharesFromDollars(50, 0.65), 0.65)).toBeCloseTo(50, 4);
  });
});

describe("computeMarketBuy", () => {
  it("returns null for an empty book", () => {
    expect(computeMarketBuy([], 50)).toBeNull();
  });

  it("caps at best_ask + SLIPPAGE_CAP", () => {
    const result = computeMarketBuy([mkLevel(0.65, 100)], 50);
    expect(result).not.toBeNull();
    expect(result!.priceCap).toBeCloseTo(0.65 + SLIPPAGE_CAP, 5);
    expect(result!.size).toBeCloseTo(50 / (0.65 + SLIPPAGE_CAP), 5);
  });

  it("clamps price cap at MAX_PROB (0.99)", () => {
    const result = computeMarketBuy([mkLevel(0.98, 100)], 50);
    expect(result!.priceCap).toBeCloseTo(MAX_PROB, 5);
  });

  it("returns null for non-positive amount", () => {
    expect(computeMarketBuy([mkLevel(0.65, 100)], 0)).toBeNull();
    expect(computeMarketBuy([mkLevel(0.65, 100)], -1)).toBeNull();
  });
});

describe("computeMarketSell", () => {
  it("returns null for an empty book", () => {
    expect(computeMarketSell([], 100)).toBeNull();
  });

  it("caps at best_bid - SLIPPAGE_CAP and size equals input shares", () => {
    const result = computeMarketSell([mkLevel(0.65, 100)], 50);
    expect(result!.priceCap).toBeCloseTo(0.65 - SLIPPAGE_CAP, 5);
    expect(result!.size).toBe(50);
  });

  it("clamps price cap at MIN_PROB (0.01)", () => {
    const result = computeMarketSell([mkLevel(0.02, 100)], 50);
    expect(result!.priceCap).toBeCloseTo(MIN_PROB, 5);
  });

  it("returns null for non-positive shares", () => {
    expect(computeMarketSell([mkLevel(0.65, 100)], 0)).toBeNull();
  });
});

describe("pickSellOutcome", () => {
  it("keeps the current outcome when the user holds it", () => {
    const holdings = new Map([["YES", 100], ["NO", 0]]);
    expect(pickSellOutcome("YES", holdings)).toBe("YES");
  });

  it("flips to the other outcome when current has zero balance", () => {
    const holdings = new Map([["YES", 0], ["NO", 50]]);
    expect(pickSellOutcome("YES", holdings)).toBe("NO");
  });

  it("returns the current outcome when nothing is held (caller surfaces error)", () => {
    const holdings = new Map([["YES", 0], ["NO", 0]]);
    expect(pickSellOutcome("YES", holdings)).toBe("YES");
  });

  it("returns the current outcome when no holdings map is supplied yet", () => {
    expect(pickSellOutcome("YES", new Map())).toBe("YES");
  });

  it("never picks an outcome with a non-positive balance", () => {
    const holdings = new Map([["YES", 0], ["NO", -5]]);
    expect(pickSellOutcome("YES", holdings)).toBe("YES");
  });
});

describe("centsToPrice", () => {
  it("converts the field's cents to the API's probability", () => {
    expect(centsToPrice("50")).toBeCloseTo(0.5);
    expect(centsToPrice("1")).toBeCloseTo(0.01);
    expect(centsToPrice("99")).toBeCloseTo(0.99);
    expect(centsToPrice("2.5")).toBeCloseTo(0.025);
  });

  it("accepts a comma as the decimal separator", () => {
    expect(centsToPrice("2,5")).toBeCloseTo(0.025);
  });

  it("yields NaN — not 0 — for empty or malformed input", () => {
    // 0 would read as a valid free order and pass the submit guard.
    expect(centsToPrice("")).toBeNaN();
    expect(centsToPrice("   ")).toBeNaN();
    expect(centsToPrice("abc")).toBeNaN();
  });

  it("keeps the ticket's own validity window intact", () => {
    // OrderTicket rejects price <= 0 or >= 1; in cents that is 0 and 100.
    expect(centsToPrice("0")).toBe(0);
    expect(centsToPrice("100")).toBe(1);
  });
});
