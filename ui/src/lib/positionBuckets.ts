/** Which of the three states a position is in.
 *
 *  A position is open, or it is closed, or it is decided but not collected.
 *  A market that resolved against the account produces no REDEEM row, so
 *  /closed-positions cannot reconstruct it and /positions keeps returning it
 *  for as long as the worthless tokens sit in the wallet — observed on
 *  production sitting under "active" at zero, priced by a market that can no
 *  longer be traded. `settled` is what separates it from a live position
 *  whose price merely happens to be low. */
export function positionBucket(p: {
  redeemable: boolean;
  settled: boolean;
}): "unclaimed" | "active" | "closed" {
  if (!p.settled) return "active";
  return p.redeemable ? "unclaimed" : "closed";
}

/** What the account is owed and has not collected, in dollars. */
export function unclaimedTotal(
  positions: readonly { redeemable: boolean; currentValue: number }[],
): number {
  return positions.reduce(
    (sum, p) => (p.redeemable ? sum + p.currentValue : sum),
    0,
  );
}

/** Which filter tab is actually showing, correcting for a selection that no
 *  longer applies. Claiming the last unclaimed position drops the total to
 *  zero and, with it, the Unclaimed button itself — staying on that tab
 *  would land on a filter with no button reading as active and nothing in
 *  the list, which reads as "something broke" rather than "you're done".
 *  Falls back to "active" for as long as there is nothing left to claim. */
export function effectivePositionFilter(
  filter: "active" | "unclaimed" | "closed",
  unclaimed: number,
): "active" | "unclaimed" | "closed" {
  return filter === "unclaimed" && unclaimed === 0 ? "active" : filter;
}

/** How a closed row reads: won or lost.
 *
 *  Keyed on the money, not on whether any came back. A position closed by
 *  selling reports its proceeds as `currentValue`, so "did anything return"
 *  was true of every sale including the losing ones -- production carried a
 *  -$600 exit wearing a green "Won", and a -$45.66 one beside it. The badge
 *  sits directly against the P/L figure and shares its colour, so the two
 *  cannot be allowed to disagree.
 */
export function closedResult(p: { cashPnl: number }): "won" | "lost" {
  return p.cashPnl >= 0 ? "won" : "lost";
}
