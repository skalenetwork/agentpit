import { type Bet, type BetFill, type BetStatus, marketCaption, type Profile } from "./api";
import { chance, clock, count, dollars, exactDollars, moment, shortDay, signedDollars, type Tone, tone } from "./format";

type Ledger = Pick<Profile, "firstTradeAt" | "valuedAt" | "money" | "bets" | "betTotals">;
type Fact = readonly [string, string];
type Caption = { readonly text: string; readonly live: boolean };

const DAY = 86_400;
const SLOTS = 40;
const PAD = 0.08;
const APART = 12;
const ORDER: readonly BetStatus[] = ["open", "won", "lost", "sold", "split"];
const CLOSED: readonly BetStatus[] = ["won", "lost", "sold", "split"];
const RESOLVED: ReadonlySet<string> = new Set(["Yes", "No", "Up", "Down"]);

const day = (at: number) => shortDay(new Date(at * 1000));
const whole = (value: number) => Math.sign(value) * Math.round(Math.abs(value));
const sum = (values: readonly number[]) => values.reduce((total, value) => total + value, 0);
const r1 = (value: number) => Math.round(value * 10) / 10;
const r2 = (value: number) => +value.toFixed(2);
const settles = (bet: Bet) => bet.kickoff ?? bet.endDate ?? Number.POSITIVE_INFINITY;

export const STATUS: Readonly<Record<BetStatus, string>> = { open: "Open", won: "Won", lost: "Lost", sold: "Sold early", split: "Split 50-50" };
export const SHOWN = 8;
export const FILLS_SHOWN = 6;
export const visible = (n: number, shown: number) => (n > shown + 2 ? shown : n);
export const moneyTone = (value: number): Tone => tone(whole(value));
export const quiet = (bet: Pick<Bet, "pnl" | "cost">) => Math.abs(bet.pnl) < Math.max(0.02 * bet.cost, 5);
export const today = ({ earned, trend }: Pick<Profile["money"], "earned" | "trend">) => earned - (trend.at(-2) ?? 0);
export const utc = (at: number, now: number) => `${moment(at, now)} UTC`;

const opening = (bet: Bet, now: number): Caption => {
  const text = marketCaption({ ...bet, eventTitle: null }, now).join(" · ");
  return { text, live: text === "Live" };
};

export interface BetTab {
  readonly status: BetStatus;
  readonly label: string;
  readonly n: number;
  readonly shown: number;
  readonly bets: readonly Bet[];
}

export const betTabs = ({ bets, betTotals }: Pick<Profile, "bets" | "betTotals">): { readonly tabs: readonly BetTab[]; readonly active: BetStatus } | null => {
  const tabs = ORDER.filter((status) => betTotals[status].n > 0).map((status) => ({ status, ...betTotals[status], shown: whole(betTotals[status].pnl) }));
  const active = bets[0]?.status;
  if (!active || !tabs.length) return null;
  const miss = (tab: (typeof tabs)[number], step: number) => Math.abs(tab.shown + step - tab.pnl) + (tab.n === 1 ? 1 : 0);
  for (let left = whole(sum(tabs.map((tab) => tab.pnl))) - sum(tabs.map((tab) => tab.shown)); left; left -= Math.sign(left)) {
    const step = Math.sign(left);
    const pick = tabs.reduce((a, b) => (miss(b, step) < miss(a, step) ? b : a));
    pick.shown += step;
  }
  return {
    tabs: tabs.map(({ status, n, shown }) => {
      const list = bets.filter((bet) => bet.status === status);
      return { status, label: status === "split" ? "Split" : STATUS[status], n, shown, bets: status === "open" ? list.toSorted((a, b) => settles(a) - settles(b)) : list };
    }),
    active,
  };
};

export const highlights = (bets: readonly Bet[]): { readonly best: Bet; readonly worst: Bet } | null => {
  const closed = bets.filter((bet) => bet.status !== "open");
  const money = (bet: Bet) => Math.round(bet.pnl);
  const [best] = closed.filter((bet) => money(bet) > 0).toSorted((a, b) => money(b) - money(a) || a.entry - b.entry);
  const [worst] = closed.filter((bet) => money(bet) < 0).toSorted((a, b) => money(a) - money(b) || b.entry - a.entry);
  return closed.length >= 3 && best && worst ? { best, worst } : null;
};

export const stakeLine = ({ cost, entry, exit, status }: Bet): { readonly stake: string; readonly price: string } => ({
  stake: `${dollars(cost)} on`,
  price: `at ${chance(entry)}${status === "open" ? (exit === null ? "" : `, now ${chance(exit)}`) : status === "sold" ? `, sold at ${chance(exit)}` : ""}`,
});

export const rowCaption = (bet: Bet, now: number): Caption =>
  bet.closedAt === null
    ? opening(bet, now)
    : { text: Math.floor(bet.closedAt / DAY) === Math.floor(now / DAY) ? clock(new Date(bet.closedAt * 1000)) : day(bet.closedAt), live: false };

export interface SeriesPoint {
  readonly at: number;
  readonly value: number;
}

export interface EarnedChart {
  readonly points: readonly (SeriesPoint & { readonly x: number; readonly y: number })[];
  readonly line: string;
  readonly area: string;
  readonly zero: number;
  readonly fill: boolean;
  readonly label: string;
}

export const earnedSeries = ({ firstTradeAt: from, valuedAt: to, money, bets, betTotals }: Ledger): readonly SeriesPoint[] => {
  const { earned, trend, trendStart } = money;
  const closed = bets.flatMap((bet) => (bet.closedAt === null ? [] : [{ at: bet.closedAt, pnl: bet.pnl }])).toReversed();
  let running = sum(CLOSED.map((status) => betTotals[status].pnl)) - sum(closed.map((bet) => bet.pnl));
  const sums = closed.map(({ at, pnl }) => {
    running += pnl;
    return { at, value: running };
  });
  const middle = sums.length ? sums : trendStart === null ? [] : trend.slice(0, -1).map((value, i) => ({ at: Date.parse(trendStart) / 1000 + (i + 1) * DAY, value }));
  const points = [{ at: from, value: 0 }, ...middle, { at: to, value: earned }].map(({ at, value }) => ({ at: Math.max(from, Math.min(at, to)), value }));
  const slot = (at: number) => Math.floor((SLOTS * (at - from)) / Math.max(1, to - from));
  return points
    .filter((point, i) => {
      const next = points[i + 1];
      return !i || !next || slot(point.at) !== slot(next.at);
    })
    .filter((point, i, kept) => point.at !== kept[i + 1]?.at);
};

export const earnedChart = (ledger: Ledger): EarnedChart => {
  const series = earnedSeries(ledger);
  const ats = series.map((point) => point.at);
  const [from, to] = [Math.min(...ats), Math.max(...ats)];
  const values = series.map((point) => point.value);
  const [top, bottom] = [Math.max(0, ...values), Math.min(0, ...values)];
  const y = (value: number) => (top === bottom ? 500 : r1(1000 * (PAD + ((1 - 2 * PAD) * (top - value)) / (top - bottom))));
  const points = series.map((point) => ({ ...point, x: r1((1000 * (point.at - from)) / Math.max(1, to - from)), y: y(point.value) }));
  const line = points.map((point, i) => `${i ? "L" : "M"}${point.x},${point.y}`).join("");
  const zero = y(0);
  const n = sum(CLOSED.map((status) => ledger.betTotals[status].n));
  return {
    points,
    line,
    area: `${line}V${zero}H0Z`,
    zero,
    fill: points.length > 3,
    label: `Earned from $0 on ${day(from)} to ${signedDollars(ledger.money.earned)} now, ${n ? `${count(n)} closed ${n === 1 ? "bet" : "bets"} added up` : "daily closes"}`,
  };
};

export const percent = (ratio: number) => {
  const value = Math.abs(ratio * 100);
  const text = value < 9.95 || (value >= 99.5 && value < 100) ? value.toFixed(1).replace(/\.0$/, "") : count(Math.round(value));
  return text === "0" ? "0%" : `${ratio > 0 ? "+" : "−"}${text}%`;
};

export const lasted = (seconds: number) => {
  const [d, h, m] = [Math.floor(seconds / DAY), Math.floor((seconds % DAY) / 3600), Math.floor((seconds % 3600) / 60)];
  return d ? `${d}d${h ? ` ${h}h` : ""}` : h ? `${h}h${m ? ` ${m}m` : ""}` : m ? `${m}m` : "Under a minute";
};

export const betCaption = (bet: Bet, now: number): Caption & { readonly event: string | null } => ({
  event: bet.eventTitle !== null && bet.eventTitle !== bet.title && !/_{3}|\.\.\./.test(bet.eventTitle) ? bet.eventTitle : null,
  ...(bet.closedAt === null ? opening(bet, now) : { text: `${bet.status === "sold" ? "Sold" : "Settled"} ${day(bet.closedAt)}`, live: false }),
});

export const thinLine = ({ status, value, sellsFor }: Bet): string | null =>
  status === "open" && sellsFor < 0.9 * value && Math.round(value) > Math.round(sellsFor) ? `Worth ${dollars(value)}, sells for ${dollars(sellsFor)} now` : null;

export const facts = (bet: Bet, now: number): readonly Fact[] => {
  const { closedAt, kickoff, endDate, winner } = bet;
  const open = closedAt === null;
  const exit = bet.exit ?? bet.entry;
  const partial = bet.sold > 0 && bet.status !== "sold";
  const early: Fact | null = partial ? ["Sold early", `${count(Math.round(bet.sold))} for ${exactDollars(sum(bet.fills.filter((fill) => fill.side === "SELL").map((fill) => fill.dollars)))}`] : null;
  const held: Fact = ["Held for", lasted((closedAt ?? now) - bet.openedAt)];
  const pays: Fact = ["Pays if right", exactDollars(bet.held)];
  const opened: Fact = ["Opened", utc(bet.openedAt, now)];
  const timing: Fact | null = kickoff !== null ? [kickoff <= now ? "Started" : "Starts", utc(kickoff, now)] : endDate !== null ? [endDate <= now ? "Ended" : "Closes", utc(endDate, now)] : null;
  const result = winner !== null ? (RESOLVED.has(winner) ? `Resolved ${winner}` : `${winner} won`) : bet.status === "split" ? "Resolved 50-50" : null;
  const exitTerm = open ? "Now" : bet.status === "sold" ? (bet.sells > 1 ? "Average sell price" : "Sold at") : partial ? "Rest settled at" : "Settled at";
  const tail: readonly (Fact | null)[] = open
    ? [early ?? pays, early && pays, early && held, opened, timing, early ? null : held]
    : [early ?? held, opened, ["Closed", utc(closedAt, now)], early && held, result === null ? null : ["Result", result]];
  const list: readonly (Fact | null)[] = [
    ["Put in", exactDollars(bet.cost)],
    open ? ["Worth now", exactDollars(bet.value)] : ["Got back", exactDollars(bet.back + bet.value)],
    ["Average price", chance(bet.entry)],
    [exitTerm, partial && !open ? `${chance(exit)} on ${count(Math.round(bet.shares - bet.sold))}` : chance(open ? bet.exit : exit)],
    ["Shares", count(Math.round(bet.shares))],
    ...tail,
  ];
  return list.filter((fact) => fact !== null);
};

export const fillCounts = (bet: Bet): readonly string[] =>
  (
    [
      [bet.buys, "buy"],
      [bet.sells, "sell"],
      [bet.fills.filter((fill) => fill.side === "REDEEM").length, "payout"],
    ] as const
  )
    .filter(([n]) => n > 0)
    .map(([n, word]) => `${count(n)} ${word}${n === 1 ? "" : "s"}`);

export const fillLine = (fill: BetFill) =>
  fill.side === "REDEEM" ? "Paid out" : `${fill.side === "SELL" ? "Sold" : "Bought"} ${count(Math.round(fill.shares))} at ${chance(fill.price)}`;

export interface PriceTag {
  readonly y: number;
  readonly text: string;
  readonly kind: "exit" | "entry" | "tick";
}

export interface PriceChart {
  readonly left: string;
  readonly line: string;
  readonly hundred: number;
  readonly zero: number;
  readonly entry: { readonly x: number; readonly y: number };
  readonly soldAvg: { readonly x: number; readonly y: number } | null;
  readonly dots: readonly { readonly x: number; readonly y: number; readonly side: "BUY" | "SELL" }[];
  readonly end: number | null;
  readonly tags: readonly PriceTag[];
  readonly legend: boolean;
  readonly right: "Now" | "Sold" | "Settled";
  readonly quiet: boolean;
  readonly label: string;
}

interface Group {
  readonly side: "BUY" | "SELL";
  readonly first: number;
  readonly at: number;
  readonly shares: number;
  readonly dollars: number;
}

export const priceChart = (bet: Bet, now: number): PriceChart => {
  const open = bet.closedAt === null;
  const sold = bet.status === "sold";
  const end = bet.closedAt ?? now;
  const exit = bet.exit ?? bet.entry;
  const history = bet.prices.filter((point) => point.at < end);
  const from = Math.min(history[0]?.at ?? bet.openedAt, bet.openedAt);
  const near = Math.max(1800, 0.015 * (end - from));
  const groups = bet.fills
    .toReversed()
    .reduce<readonly Group[]>((out, fill) => {
      if (fill.side === "REDEEM") return out;
      const last = out.at(-1);
      return last && last.side === fill.side && fill.at - last.first < near
        ? out.with(-1, { ...last, at: fill.at, shares: last.shares + fill.shares, dollars: last.dollars + fill.dollars })
        : [...out, { side: fill.side, first: fill.at, at: fill.at, shares: fill.shares, dollars: fill.dollars }];
    }, [])
    .map((group) => ({ ...group, price: group.dollars / group.shares }));
  const [first] = groups;
  const sells = groups.filter((group) => group.side === "SELL");
  const tail = sold ? [] : [{ at: end, price: exit }];
  const anchors = [...groups, ...tail].map((point) => point.at);
  const points = [...history.filter((point) => anchors.every((at) => Math.abs(at - point.at) >= near)), ...groups, ...tail].toSorted((a, b) => a.at - b.at);
  const x = (at: number) => r2((100 * (at - from)) / Math.max(1, end - from));
  const y = (price: number) => r2(100 * (0.06 + 0.88 * (1 - price)));
  const [ye, ya] = [y(exit), y(bet.entry)];
  const mid = Math.min(100 - APART / 2 - 3, Math.max(APART / 2 + 3, (ye + ya) / 2));
  const [le, la] = Math.abs(ye - ya) >= APART ? [ye, ya] : ye <= ya ? [mid - APART / 2, mid + APART / 2] : [mid + APART / 2, mid - APART / 2];
  return {
    left: day(from),
    line: points.map((point, i) => `${i ? "L" : "M"}${r1(x(point.at) * 10)},${r1(y(point.price) * 10)}`).join(""),
    hundred: y(1),
    zero: y(0),
    entry: { x: x(first?.at ?? bet.openedAt), y: ya },
    soldAvg: sold && sells.length > 1 && sells[0] ? { x: x(sells[0].at), y: ye } : null,
    dots: groups.filter((group) => sold || !(x(group.at) > 98.5 && Math.abs(y(group.price) - ye) < 2)).map((group) => ({ x: x(group.at), y: y(group.price), side: group.side })),
    end: sold ? null : ye,
    tags: [
      { y: r2(le), text: sold ? `${sells.length > 1 ? "avg sold" : "sold"} ${chance(exit)}` : open ? `now ${chance(exit)}` : chance(exit), kind: "exit" },
      { y: r2(la), text: `${bet.buys === 1 ? "bought" : "avg buy"} ${chance(bet.entry)}`, kind: "entry" },
      ...[1, 0].filter((price) => Math.abs(y(price) - le) >= APART && Math.abs(y(price) - la) >= APART).map((price) => ({ y: y(price), text: `${price * 100}%`, kind: "tick" as const })),
    ],
    legend: sells.length > 0,
    right: open ? "Now" : sold ? "Sold" : "Settled",
    quiet: quiet(bet),
    label: `${bet.outcome} price from ${chance(points[0]?.price ?? exit)} on ${day(from)} to ${chance(exit)} ${open ? "now" : sold ? "when sold" : "at the result"}, bought at ${chance(bet.entry)} on average`,
  };
};
