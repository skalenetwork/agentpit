/** The fields the positions header can sort on. Declared structurally rather
 *  than as `Position` from `@/api/portfolio` so the sort can be exercised with
 *  four-field literals in tests and never has to follow the wire type as it
 *  grows. */
export interface SortablePosition {
  title: string;
  initialValue: number;
  sellableValue: number;
  cashPnl: number;
  /** Token id — unique per row, and the tie-break. */
  asset: string;
}

/** A sortable positions column. Sorting is client-side: `/positions` returns
 *  the whole list in one payload, so re-ordering costs no round trip. */
export type PositionColumn = "market" | "cost" | "value" | "pnl";

export interface PositionSort {
  column: PositionColumn;
  dir: "desc" | "asc";
}

/** The list's opening order: the biggest holdings first, measured by what the
 *  book would actually pay for them. */
export const DEFAULT_POSITION_SORT: PositionSort = {
  column: "value",
  dir: "desc",
};

/** Clicking a column sorts by it, biggest first; clicking the SAME column
 *  again flips the direction. Mirrors `nextBoardSort` in
 *  `@/api/leaderboard` — the Arena header behaves this way and the two
 *  headers should not need separate explaining. */
export function nextPositionSort(
  current: PositionSort,
  column: PositionColumn,
): PositionSort {
  if (current.column !== column) return { column, dir: "desc" };
  return { column, dir: current.dir === "desc" ? "asc" : "desc" };
}

function numericValue(row: SortablePosition, column: PositionColumn): number {
  if (column === "cost") return row.initialValue;
  if (column === "value") return row.sellableValue;
  return row.cashPnl;
}

/** A new array, ordered by `sort`; the input is never mutated.
 *
 *  "market" compares titles case-insensitively — the catalogue mixes
 *  sentence-case and upper-case titles, and a raw code-unit compare would file
 *  every upper-case one ahead of the rest. Its A-to-Z reading is *ascending*,
 *  so a first click (descending) lands on Z-to-A, the same way a first click
 *  on a money column lands on the largest number.
 *
 *  Ties break on `asset` so rows keep a fixed order across the poll instead of
 *  swapping places on every refetch — several positions can share a cost, a
 *  value, or a P/L of exactly zero. */
export function sortPositions<T extends SortablePosition>(
  rows: ReadonlyArray<T>,
  sort: PositionSort,
): T[] {
  const sign = sort.dir === "desc" ? -1 : 1;
  return [...rows].sort((a, b) => {
    if (sort.column === "market") {
      const cmp = a.title.localeCompare(b.title, undefined, {
        sensitivity: "base",
      });
      if (cmp !== 0) return sign * cmp;
      return a.asset.localeCompare(b.asset);
    }
    const av = numericValue(a, sort.column);
    const bv = numericValue(b, sort.column);
    if (av !== bv) return sign * (av - bv);
    return a.asset.localeCompare(b.asset);
  });
}
