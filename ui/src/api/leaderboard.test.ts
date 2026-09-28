import { describe, expect, it } from "vitest";
import {
  DEFAULT_BOARD_SORT,
  findMyRank,
  isSameAddress,
  nextBoardSort,
  sortBoard,
  boardTrendPoints,
  boardViewState,
  formatBoardAmount,
  trendTone,
  type BoardEntry,
  type BoardResponse,
} from "./leaderboard";

describe("leaderboard board data", () => {
  it("renders base-unit strings as dollars", () => {
    expect(formatBoardAmount("100000000000")).toBe("$100,000.00");
    expect(formatBoardAmount("-2500000")).toBe("-$2.50");
  });
});

function boardEntry(over: Partial<BoardEntry>): BoardEntry {
  return {
    rank: 1,
    name: "degen_trader",
    address: "0x7aD82E9a000000000000000000000000000000",
    capital: "150000000000",
    earned: "50000000000",
    invested: "1000000000",
    returnPct: 50,
    trades: 12,
    ...over,
  } as BoardEntry;
}

function boardResponse(entries: BoardEntry[]): BoardResponse {
  return { sort: "return", entries };
}

describe("boardViewState", () => {
  it("is loading before the first response and before any error", () => {
    expect(boardViewState(undefined, null, [])).toBe("loading");
  });

  it("is error only when the first load has failed with nothing to show", () => {
    expect(boardViewState(undefined, new Error("network"), [])).toBe("error");
  });

  it("is rows once data has arrived", () => {
    const entries = [boardEntry({})];
    expect(boardViewState(boardResponse(entries), null, entries)).toBe("rows");
  });

  it("is empty once data has arrived with no entries", () => {
    expect(boardViewState(boardResponse([]), null, [])).toBe("empty");
  });

  it("keeps showing rows when a background refetch fails but data survives — the case that matters", () => {
    const entries = [boardEntry({})];
    expect(
      boardViewState(boardResponse(entries), new Error("transient"), entries),
    ).toBe("rows");
  });

  it("keeps showing empty (not error) when a background refetch fails after a real empty board", () => {
    expect(boardViewState(boardResponse([]), new Error("transient"), [])).toBe(
      "empty",
    );
  });
});

describe("boardTrendPoints", () => {
  it("plots earned at each daily close, in order", () => {
    expect(boardTrendPoints(["0", "50000000000"])).toEqual([
      { t: 0, p: 0 },
      { t: 1, p: 50_000_000_000 },
    ]);
  });

  it("pads a single point so a fresh account draws a flat line, not a dot", () => {
    expect(boardTrendPoints(["0"])).toHaveLength(2);
  });

  it("is empty without a close", () => {
    expect(boardTrendPoints([])).toEqual([]);
  });
});

describe("trendTone", () => {
  it("matches the sign convention the Return column already uses", () => {
    expect(trendTone(12.5)).toBe("up");
    expect(trendTone(-4)).toBe("down");
    expect(trendTone(0)).toBe("neutral");
  });
});

describe("board sorting", () => {
  const rows = [
    boardEntry({ address: "0xa", unrealized: "300", trades: 1 }),
    boardEntry({ address: "0xb", unrealized: "100", trades: 9 }),
    boardEntry({ address: "0xc", unrealized: "200", trades: 5 }),
  ];

  it("opens on the biggest paper gain", () => {
    expect(DEFAULT_BOARD_SORT).toEqual({ column: "unrealized", dir: "desc" });
  });

  it("sorts descending by the chosen column", () => {
    expect(
      sortBoard(rows, { column: "unrealized", dir: "desc" }).map((r) => r.address),
    ).toEqual(["0xa", "0xc", "0xb"]);
  });

  it("sorts ascending when asked", () => {
    expect(
      sortBoard(rows, { column: "trades", dir: "asc" }).map((r) => r.address),
    ).toEqual(["0xa", "0xc", "0xb"]);
  });

  it("breaks ties on address so rows do not swap between polls", () => {
    const tied = [
      boardEntry({ address: "0xz", unrealized: "5" }),
      boardEntry({ address: "0xa", unrealized: "5" }),
    ];
    const order = { column: "unrealized", dir: "desc" } as const;
    expect(sortBoard(tied, order).map((r) => r.address)).toEqual(["0xa", "0xz"]);
    expect(sortBoard(sortBoard(tied, order), order).map((r) => r.address)).toEqual(
      ["0xa", "0xz"],
    );
  });

  it("leaves the input array untouched", () => {
    const before = rows.map((r) => r.address);
    sortBoard(rows, { column: "trades", dir: "asc" });
    expect(rows.map((r) => r.address)).toEqual(before);
  });

  it("handles negative amounts, which paper losses are", () => {
    const withLoss = [
      boardEntry({ address: "0xa", unrealized: "-500" }),
      boardEntry({ address: "0xb", unrealized: "100" }),
    ];
    expect(
      sortBoard(withLoss, { column: "unrealized", dir: "desc" }).map((r) => r.address),
    ).toEqual(["0xb", "0xa"]);
  });
});

describe("nextBoardSort", () => {
  it("starts a new column descending — nobody opens a board to see who is last", () => {
    expect(nextBoardSort({ column: "trades", dir: "asc" }, "realized")).toEqual({
      column: "realized",
      dir: "desc",
    });
  });

  it("flips direction when the same column is clicked again", () => {
    const first = nextBoardSort(DEFAULT_BOARD_SORT, "capital");
    expect(first).toEqual({ column: "capital", dir: "desc" });
    expect(nextBoardSort(first, "capital")).toEqual({
      column: "capital",
      dir: "asc",
    });
    expect(nextBoardSort(nextBoardSort(first, "capital"), "capital")).toEqual({
      column: "capital",
      dir: "desc",
    });
  });
});

describe("finding yourself on the board", () => {
  const rows = [
    boardEntry({ address: "0xAAA1", unrealized: "300" }),
    boardEntry({ address: "0xBbB2", unrealized: "200" }),
    boardEntry({ address: "0xCcC3", unrealized: "100" }),
  ];

  it("matches addresses regardless of case", () => {
    // The board sends checksummed addresses; the session may hold either form.
    // A raw === would leave the reader unable to find themselves.
    expect(isSameAddress("0xAbC", "0xabc")).toBe(true);
    expect(isSameAddress("0xAbC", "0xdef")).toBe(false);
  });

  it("treats a missing address as no match rather than a false one", () => {
    expect(isSameAddress(undefined, "0xabc")).toBe(false);
    expect(isSameAddress("0xabc", undefined)).toBe(false);
    expect(isSameAddress(undefined, undefined)).toBe(false);
  });

  it("reports the position in the CURRENT order, not the server rank", () => {
    expect(findMyRank(rows, "0xbbb2")).toBe(2);
    expect(findMyRank(sortBoard(rows, { column: "unrealized", dir: "asc" }), "0xbbb2")).toBe(2);
    expect(findMyRank(sortBoard(rows, { column: "unrealized", dir: "asc" }), "0xaaa1")).toBe(3);
  });

  it("returns null when signed out", () => {
    expect(findMyRank(rows, undefined)).toBeNull();
  });

  it("returns null for an account that has never traded", () => {
    expect(findMyRank(rows, "0xnothere")).toBeNull();
  });
});
