/** Locale formatters + small wrappers used across cards and detail pages.
 *
 *  Keep these in one place so a label like "May 19" or a volume like "$8.1M"
 *  renders identically wherever it appears. Wrappers accept Unix-seconds
 *  inputs (the shape every market endpoint returns) and return `null` for
 *  null inputs so call sites don't have to guard.
 */

import type { MarketState } from "@/types/market";

const SHORT_DATE_FMT = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
});

const LONG_DATE_FMT = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  year: "numeric",
});

/** "May 19" — used on grid cards and chart axes. */
export function formatShortDate(seconds: number | null): string | null {
  if (seconds === null) return null;
  return SHORT_DATE_FMT.format(new Date(seconds * 1000));
}

/** "May 19, 2026" — used in the event/market detail header. */
export function formatLongDate(seconds: number | null): string | null {
  if (seconds === null) return null;
  return LONG_DATE_FMT.format(new Date(seconds * 1000));
}

/** What a card prints where a closing date goes.
 *
 *  A market can trade past its own stated deadline — Polymarket keeps the book
 *  open while the question stays open — and printing "closes Jun 1" beside a
 *  live order book makes the card contradict itself.
 *
 *  The test is deliberately date AND state, never date alone: 849 events on
 *  production are past-dated and fully resolved, and for those the date is the
 *  right thing to show.
 *
 *  `formatDate` defaults to the short grid-card format; the detail pages pass
 *  their own long-date formatter so this stays the one place "is this overdue
 *  and live" gets decided, while each caller keeps its own date shape. */
export function closeLabel(
  endDate: number | null,
  state: MarketState,
  nowSeconds: number,
  formatDate: (seconds: number) => string | null = formatShortDate,
): { prefix: string | null; value: string } | null {
  if (endDate === null) return null;
  if (endDate < nowSeconds && state === "ACTIVE") {
    // The deadline lapsed, not the market: upstream keeps the book open while
    // the question stays open. Keep the date — how far it has slipped is
    // information — and drop the claim that anything is closing.
    const overdue = formatDate(endDate);
    return overdue === null ? null : { prefix: "overdue", value: overdue };
  }
  const value = formatDate(endDate);
  // `value` is only ever null here if a caller's formatter returns null for a
  // non-null input, which none does — kept for the TS narrowing below.
  return value === null ? null : { prefix: "closes", value };
}

const CLOCK_FMT = new Intl.DateTimeFormat("en-US", {
  hour: "numeric",
  minute: "2-digit",
});

/** "12:50 PM" — used to label rotating-series time windows. */
export function formatClock(seconds: number | null): string | null {
  if (seconds === null) return null;
  return CLOCK_FMT.format(new Date(seconds * 1000));
}

/** "4:03" — a mm:ss countdown from a non-negative second count. */
export function formatCountdown(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return `${m}:${String(rem).padStart(2, "0")}`;
}

/** Parse a stringified upstream volume ("0", "14646954.7", "") into a number.
 *  Returns null for absent/zero/unparseable values so call sites can hide the
 *  stat rather than render a misleading "$0". */
export function parseVolume(raw: string | null | undefined): number | null {
  if (raw == null) return null;
  const n = Number(raw);
  if (!Number.isFinite(n) || n <= 0) return null;
  return n;
}

/** Compact dollar formatter — $850, $12.4K, $8.1M, $1.2B, $3.4T, $1.0Q.
 *  Goes up to quadrillions for upstream Polymarket market volumes. (It used to
 *  be sized for the old quadrillion-apUSD signup grant; that grant is $100k
 *  now, but the large branches still earn their keep on volume figures.) */
export function formatVolume(usd: number): string {
  if (usd >= 1e15) return `$${(usd / 1e15).toFixed(1)}Q`;
  if (usd >= 1e12) return `$${(usd / 1e12).toFixed(1)}T`;
  if (usd >= 1_000_000_000) return `$${(usd / 1_000_000_000).toFixed(1)}B`;
  if (usd >= 1_000_000) return `$${(usd / 1_000_000).toFixed(1)}M`;
  if (usd >= 1_000) return `$${(usd / 1_000).toFixed(1)}K`;
  return `$${Math.round(usd)}`;
}

/** Format a YES mid-price (dollars in [0, 1]) as a whole-percent probability
 *  label, without the "%" sign. A non-zero probability below 1% renders as
 *  "<1" rather than rounding down to a misleading "0"; a null mid renders as
 *  an em dash. Call sites append their own "%". */
export function formatProbabilityPct(mid: number | null): string {
  if (mid === null) return "—";
  const pct = mid * 100;
  if (pct <= 0) return "0";
  if (pct < 1) return "<1";
  return String(Math.round(pct));
}

const SIGNED_USD_FMT = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

/** "+$84.20" / "-$210.80" / "$0.00" — a P/L dollar amount with an explicit
 *  leading sign for non-zero values, so a gain reads unambiguously on the
 *  arena leaderboard. */
export function formatSignedUsd(n: number): string {
  const body = SIGNED_USD_FMT.format(Math.abs(n));
  if (n > 0) return `+${body}`;
  if (n < 0) return `-${body}`;
  return body;
}

/** "42s" / "5m" / "2h" / "3d" — coarse age of an event given its distance in
 *  seconds. Negative distances (clock skew) clamp to "0s". Callers append
 *  their own "ago". */
export function relativeTime(secsAgo: number): string {
  if (secsAgo < 0) secsAgo = 0;
  if (secsAgo < 60) return `${secsAgo}s`;
  if (secsAgo < 3600) return `${Math.floor(secsAgo / 60)}m`;
  if (secsAgo < 86_400) return `${Math.floor(secsAgo / 3600)}h`;
  return `${Math.floor(secsAgo / 86_400)}d`;
}

/** Which volume figure a card should show, and what to call it.
 *
 *  `prefer` follows the list's sort, so the number on a card explains the order
 *  it is in. Under "24hr Volume" a card showing its all-time figure reads as a
 *  contradiction — $16.2M sitting above $12.0M above $1.4M, in a list that says
 *  it is ranked by the last day.
 *
 *  Either figure falls back to the other, because only events touched by a
 *  recent sync carry both: one that has dropped out of the synced top-N keeps
 *  its last-captured 24h figure and never gets an all-time one. Showing that —
 *  labelled as what it is — beats dropping the line, which would have blanked
 *  545 of 962 production events. */
export function volumeStat(
  volume: number | null,
  volume24hr: number | null,
  prefer: "total" | "24h" = "total",
): { value: number; label: string } | null {
  const total = volume !== null ? { value: volume, label: "vol" } : null;
  const daily =
    volume24hr !== null ? { value: volume24hr, label: "24h vol" } : null;
  return prefer === "24h" ? (daily ?? total) : (total ?? daily);
}

/** "3.3" / "25" / "-21.9" — a P/L percentage at one decimal, dropping a
 *  trailing ".0" so a clean value still reads as "25".
 *
 *  Rounding to a whole percent made the parenthetical contradict the dollars
 *  beside it: a $0.30 gain on a $9.20 cost is 3.26%, and "+$0.30 (3%)" invites
 *  the reader to compute 9.20 x 1.03 = $9.48 and wonder where the $9.50 came
 *  from. The sign is preserved; callers that print their own sign should pass
 *  an absolute value. */
export function formatPnlPct(pct: number): string {
  if (!Number.isFinite(pct)) return "0";
  const rounded = Math.round(pct * 10) / 10;
  return Number.isInteger(rounded) ? String(rounded) : rounded.toFixed(1);
}

const WEI_PER_NATIVE = 1_000_000_000_000_000_000n;

/** Four decimals: the signup grant is 0.02 native (`signup_gas_grant_wei`),
 *  so two decimals rounds most real balances straight to "0.00" long before
 *  the wallet is actually out of gas. This is the smallest fixed precision
 *  that still shows life at that scale. */
const CREDITS_DECIMALS = 4n;
const CREDITS_SCALE = 10n ** CREDITS_DECIMALS;

/** "0.0134" — a wei string (from `/me/credits`) as a native-coin figure,
 *  precise enough to stay useful at the signup-grant's own scale.
 *
 *  BigInt arithmetic throughout, never `Number`: the whole point of sending
 *  wei as a string is that it can exceed Number's safe integer range, and
 *  routing it through a float on the way to four decimals would silently
 *  reintroduce the precision loss the string was chosen to avoid. Rounds
 *  half up at the fourth decimal rather than truncating. */
export function formatCredits(weiString: string): string {
  // `BigInt("")` returns 0n rather than throwing, so an empty string needs
  // its own guard before the try/catch below can catch everything else.
  if (weiString.trim() === "") return "—";
  let wei: bigint;
  try {
    wei = BigInt(weiString);
  } catch {
    return "—";
  }
  const negative = wei < 0n;
  if (negative) wei = -wei;
  const scaledUnits =
    (wei * CREDITS_SCALE + WEI_PER_NATIVE / 2n) / WEI_PER_NATIVE;
  const whole = scaledUnits / CREDITS_SCALE;
  const frac = scaledUnits % CREDITS_SCALE;
  const body = `${whole}.${frac.toString().padStart(Number(CREDITS_DECIMALS), "0")}`;
  return negative ? `-${body}` : body;
}

/** "0.0134" / "1" / "0" — a wei string as its exact native-coin figure, with
 *  no rounding and no trailing zeros. For the tooltip on the Credits cell,
 *  the same role `USD.format(balance)` plays for the apUSD cell: the
 *  abbreviated/fixed-precision headline value stays scannable, and hovering
 *  reveals precisely what's there down to the last wei. */
export function formatCreditsExact(weiString: string): string {
  if (weiString.trim() === "") return "—";
  let wei: bigint;
  try {
    wei = BigInt(weiString);
  } catch {
    return "—";
  }
  const negative = wei < 0n;
  if (negative) wei = -wei;
  const whole = wei / WEI_PER_NATIVE;
  const frac = wei % WEI_PER_NATIVE;
  const fracStr = frac.toString().padStart(18, "0").replace(/0+$/, "");
  const body = fracStr ? `${whole}.${fracStr}` : `${whole}`;
  return negative ? `-${body}` : body;
}

/** "0x933B…5215" — an address short enough to sit on one line without
 *  wrapping, keeping both ends so it stays recognisable at a glance. The full
 *  value is never far: every place this renders offers a way to copy it.
 *
 *  Shared rather than per-page: the Arena board and the profile show the same
 *  account, and two truncation rules would make it look like two accounts. */
export function shortAddress(a: string): string {
  return a.length < 12 ? a : `${a.slice(0, 6)}…${a.slice(-4)}`;
}

/** Labels title-casing gets wrong because they are acronyms, not words.
 *
 *  Only entries actually observed arriving lowercase from upstream, plus the
 *  league abbreviations that would land here the same way — a speculative list
 *  would rot, since upstream mints new tags constantly and most already arrive
 *  correctly cased ("ATP Tour", "MLB", "AI"). Keyed on the full lowercase
 *  label, so a multi-word label can map too. */
const TAG_ACRONYMS = new Map<string, string>([
  ["fomc", "FOMC"],
  ["lol", "LoL"],
  ["val", "VAL"],
  ["nba", "NBA"],
  ["nfl", "NFL"],
  ["mlb", "MLB"],
  ["nhl", "NHL"],
  ["ufc", "UFC"],
  ["mma", "MMA"],
  ["ai", "AI"],
  ["etf", "ETF"],
  ["ipo", "IPO"],
  ["gdp", "GDP"],
  ["cpi", "CPI"],
  ["btc", "BTC"],
  ["eth", "ETH"],
  ["xrp", "XRP"],
]);

/** Small words that stay lowercase inside a title, unless they lead it. */
const TITLE_STOPWORDS = new Set([
  "a", "an", "and", "at", "by", "for", "in", "of", "on", "or", "the", "to", "vs",
]);

/** A tag label, cased for display.
 *
 *  Upstream authors these by hand and is inconsistent: most read like titles
 *  ("Global Elections", "ATP Tour", "Argentina Primera División") but a
 *  handful arrive entirely lowercase ("league of legends", "primary
 *  elections", "baseball") and look like a bug beside their neighbours.
 *
 *  Only the all-lowercase ones are touched. Any label carrying a capital was
 *  cased deliberately, and re-casing it would wreck the acronyms and proper
 *  nouns that make up most of the list — "AI", "ATP", "A100", "US Election"
 *  would come back as "Ai", "Atp", "A100", "Us Election".
 */
export function displayTagLabel(label: string): string {
  if (/[A-Z]/.test(label)) return label;
  const acronym = TAG_ACRONYMS.get(label);
  if (acronym) return acronym;
  return label
    .split(" ")
    .map((word, i) =>
      i > 0 && TITLE_STOPWORDS.has(word)
        ? word
        : word.charAt(0).toUpperCase() + word.slice(1),
    )
    .join(" ");
}

const SHARE_COUNT = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 0,
  maximumFractionDigits: 2,
});

/** A share quantity as a person reads it.
 *
 *  A market order's size is a division -- dollars over the price cap -- so it
 *  arrives as 1.4084507042253522, and the trade button was printing every one
 *  of those digits back at the trader. Two decimals is also the size
 *  precision the exchange itself works in.
 *
 *  Whole numbers keep no decimals: a limit order for twelve shares should
 *  read "12", not "12.00".
 */
export function formatShares(shares: number): string {
  return SHARE_COUNT.format(shares);
}
