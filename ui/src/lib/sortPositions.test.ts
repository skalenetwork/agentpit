import { describe, expect, it } from "vitest";
import {
  DEFAULT_POSITION_SORT,
  nextPositionSort,
  sortPositions,
  type SortablePosition,
} from "./sortPositions";

function row(over: Partial<SortablePosition> = {}): SortablePosition {
  return {
    title: "Market",
    initialValue: 0,
    sellableValue: 0,
    cashPnl: 0,
    asset: "0x0",
    ...over,
  };
}

const titles = (rows: SortablePosition[]) => rows.map((r) => r.title);
const assets = (rows: SortablePosition[]) => rows.map((r) => r.asset);

describe("DEFAULT_POSITION_SORT", () => {
  it("opens on the largest sellable value", () => {
    expect(DEFAULT_POSITION_SORT).toEqual({ column: "value", dir: "desc" });
  });
});

describe("nextPositionSort", () => {
  it("sorts a newly picked column descending", () => {
    expect(nextPositionSort({ column: "value", dir: "asc" }, "pnl")).toEqual({
      column: "pnl",
      dir: "desc",
    });
  });

  it("flips the direction when the same column is clicked again", () => {
    expect(nextPositionSort({ column: "pnl", dir: "desc" }, "pnl")).toEqual({
      column: "pnl",
      dir: "asc",
    });
    expect(nextPositionSort({ column: "pnl", dir: "asc" }, "pnl")).toEqual({
      column: "pnl",
      dir: "desc",
    });
  });

  // The market column is text, but a first click still means "descending" —
  // the header only ever hands back one of the two directions.
  it("starts the market column descending like any other", () => {
    expect(nextPositionSort({ column: "cost", dir: "asc" }, "market")).toEqual({
      column: "market",
      dir: "desc",
    });
  });
});

describe("sortPositions", () => {
  it("orders the value column by what the book would pay", () => {
    const rows = [
      row({ title: "a", sellableValue: 5, asset: "0xa" }),
      row({ title: "b", sellableValue: 90, asset: "0xb" }),
      row({ title: "c", sellableValue: 40, asset: "0xc" }),
    ];
    expect(
      titles(sortPositions(rows, { column: "value", dir: "desc" })),
    ).toEqual(["b", "c", "a"]);
    expect(
      titles(sortPositions(rows, { column: "value", dir: "asc" })),
    ).toEqual(["a", "c", "b"]);
  });

  it("orders the cost column by the amount put to work", () => {
    const rows = [
      row({ title: "a", initialValue: 10, sellableValue: 999, asset: "0xa" }),
      row({ title: "b", initialValue: 70, sellableValue: 1, asset: "0xb" }),
    ];
    expect(
      titles(sortPositions(rows, { column: "cost", dir: "desc" })),
    ).toEqual(["b", "a"]);
  });

  // Losses are negative, so descending must put the winner on top rather than
  // ordering by magnitude.
  it("orders the p/l column with the winners above the losers", () => {
    const rows = [
      row({ title: "loser", cashPnl: -80, asset: "0xa" }),
      row({ title: "flat", cashPnl: 0, asset: "0xb" }),
      row({ title: "winner", cashPnl: 12, asset: "0xc" }),
    ];
    expect(titles(sortPositions(rows, { column: "pnl", dir: "desc" }))).toEqual(
      ["winner", "flat", "loser"],
    );
  });

  it("reads the market column A-to-Z when it is ascending", () => {
    const rows = [
      row({ title: "Zebra", asset: "0xa" }),
      row({ title: "apple", asset: "0xb" }),
      row({ title: "Mango", asset: "0xc" }),
    ];
    expect(
      titles(sortPositions(rows, { column: "market", dir: "asc" })),
    ).toEqual(["apple", "Mango", "Zebra"]);
    expect(
      titles(sortPositions(rows, { column: "market", dir: "desc" })),
    ).toEqual(["Zebra", "Mango", "apple"]);
  });

  // The catalogue mixes sentence-case and upper-case titles; a code-unit
  // compare would file every upper-case title ahead of every lower-case one.
  it("ignores case when comparing titles", () => {
    const rows = [
      row({ title: "banana", asset: "0xa" }),
      row({ title: "APPLE", asset: "0xb" }),
    ];
    expect(
      titles(sortPositions(rows, { column: "market", dir: "asc" })),
    ).toEqual(["APPLE", "banana"]);
  });

  // Several positions share a P/L of exactly zero. Without the tie-break they
  // would trade places between polls and the list would jitter under the cursor.
  it("breaks numeric ties on the asset so the order survives a refetch", () => {
    const rows = [
      row({ asset: "0xc" }),
      row({ asset: "0xa" }),
      row({ asset: "0xb" }),
    ];
    for (const dir of ["desc", "asc"] as const) {
      expect(assets(sortPositions(rows, { column: "pnl", dir }))).toEqual([
        "0xa",
        "0xb",
        "0xc",
      ]);
    }
  });

  it("breaks title ties on the asset too", () => {
    const rows = [
      row({ title: "Same market", asset: "0xb" }),
      row({ title: "same market", asset: "0xa" }),
    ];
    expect(
      assets(sortPositions(rows, { column: "market", dir: "desc" })),
    ).toEqual(["0xa", "0xb"]);
  });

  it("returns a new array and leaves the input untouched", () => {
    const rows = [
      row({ title: "a", sellableValue: 1, asset: "0xa" }),
      row({ title: "b", sellableValue: 2, asset: "0xb" }),
    ];
    const sorted = sortPositions(rows, DEFAULT_POSITION_SORT);
    expect(sorted).not.toBe(rows);
    expect(titles(rows)).toEqual(["a", "b"]);
    expect(titles(sorted)).toEqual(["b", "a"]);
  });

  it("sorts an empty list to an empty list", () => {
    expect(sortPositions([], DEFAULT_POSITION_SORT)).toEqual([]);
  });

  // The header drives this with the full Position rows; the extra fields must
  // survive the sort rather than being narrowed away.
  it("keeps the caller's own row shape", () => {
    const rows = [
      { ...row({ sellableValue: 1, asset: "0xa" }), redeemable: true },
      { ...row({ sellableValue: 9, asset: "0xb" }), redeemable: false },
    ];
    const [first] = sortPositions(rows, DEFAULT_POSITION_SORT);
    expect(first?.redeemable).toBe(false);
  });
});
