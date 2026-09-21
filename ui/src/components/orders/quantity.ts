/** What the Shares and Amount fields will hold.
 *
 *  Both used to put whatever was typed straight into state, so a field
 *  measured in shares would happily read "вавыа" and only fail on submit —
 *  the same defect the limit-price field had, and fixed the same way:
 *  normalised on the way in, not on submit.
 *
 *  Unlike a price this has no ceiling; what it has is a floor of zero (there
 *  is no negative order) and two decimals, which is the size precision the
 *  exchange itself works in.
 */
export const MAX_QUANTITY_DECIMALS = 2;

/** Digits with at most one decimal point, and nothing else — no sign, no
 *  exponent, no second point. */
const SHAPE = /^\d*\.?\d*$/;

/** What the field should show once the user has typed `raw` into it.
 *
 *  Input that cannot be read as a positive decimal leaves the field as it
 *  was, so the keystroke is refused rather than stored and rejected later.
 *  A trailing point survives: it is how "12.5" gets typed.
 */
export function normaliseQuantity(raw: string, previous: string): string {
  const typed = raw.trim().replace(/,/g, ".");
  if (typed === "") return "";
  if (!SHAPE.test(typed)) return previous;

  const point = typed.indexOf(".");
  let whole = point === -1 ? typed : typed.slice(0, point);
  const fraction = point === -1 ? "" : typed.slice(point + 1);
  if (fraction.length > MAX_QUANTITY_DECIMALS) return previous;

  // "007" is 7, "00" is 0, and a lone "0" stands — it is how a fraction of a
  // share starts.
  whole = whole.replace(/^0+(?=\d)/, "");
  if (whole === "") whole = "0";

  return point === -1 ? whole : `${whole}.${fraction}`;
}
