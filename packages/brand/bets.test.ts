import { expect, test } from "bun:test";
import type { Bet, BetFill, BetStatus, BetTotal, Profile } from "./api";
import {
  betCaption,
  betTabs,
  earnedChart,
  earnedSeries,
  facts,
  fillCounts,
  fillLine,
  highlights,
  lasted,
  moneyTone,
  percent,
  priceChart,
  quiet,
  rowCaption,
  stakeLine,
  thinLine,
  today,
  utc,
  visible,
} from "./bets";

const NOW = Date.UTC(2026, 9, 22, 11, 12) / 1000;
const HOUR = 3600;

const fill = (at: number, side: BetFill["side"], shares: number, price: number | null, dollars: number): BetFill => ({ at, side, shares, price, dollars, tx: null });

const bet = (change: Partial<Bet> = {}): Bet => ({
  id: "1",
  title: "Will it rain in Kyiv?",
  icon: null,
  url: null,
  category: null,
  outcome: "Yes",
  status: "open",
  openedAt: NOW - 28_200,
  closedAt: null,
  buys: 1,
  sells: 0,
  entry: 0.5,
  exit: 0.37,
  shares: 3000,
  sold: 0,
  held: 3000,
  cost: 1500,
  back: 0,
  value: 1110,
  sellsFor: 1090,
  pnl: -390,
  fills: [fill(NOW - 28_200, "BUY", 3000, 0.5, 1500)],
  prices: [],
  eventTitle: null,
  endDate: null,
  resolvedAt: null,
  winner: null,
  kickoff: null,
  trading: null,
  ...change,
});

const closed = (status: BetStatus, closedAt: number, pnl: number, change: Partial<Bet> = {}) =>
  bet({ status, closedAt, pnl, exit: status === "won" ? 1 : status === "lost" ? 0 : 0.5, value: 0, ...change });

const totals = (parts: Partial<Record<BetStatus, BetTotal>>): Profile["betTotals"] => ({
  open: { n: 0, pnl: 0 },
  won: { n: 0, pnl: 0 },
  lost: { n: 0, pnl: 0 },
  sold: { n: 0, pnl: 0 },
  split: { n: 0, pnl: 0 },
  ...parts,
});

const money = (earned: number, trend: readonly number[] = [], trendStart: string | null = null): Profile["money"] => ({
  returnPct: 0,
  capital: 100_000 + earned,
  deposited: 100_000,
  earned,
  trendStart,
  trend,
});

test("tabs follow the status order, add up to their rounded total and spend the rounding on a tab of several bets", () => {
  const bets = [closed("won", NOW, 10.4, { id: "w" }), closed("sold", NOW - 1, 0.2, { id: "s1" }), closed("sold", NOW - 2, 0.2, { id: "s2" }), closed("split", NOW - 3, 0.4, { id: "p" })];
  const tabs = betTabs({ bets, betTotals: totals({ won: { n: 1, pnl: 10.4 }, sold: { n: 2, pnl: 0.4 }, split: { n: 1, pnl: 0.4 } }) });
  expect(tabs?.tabs.map(({ status, label, n, shown }) => [status, label, n, shown])).toEqual([
    ["won", "Won", 1, 10],
    ["sold", "Sold early", 2, 1],
    ["split", "Split", 1, 0],
  ]);
  expect(tabs?.tabs.map((tab) => tab.bets.map((b) => b.id))).toEqual([["w"], ["s1", "s2"], ["p"]]);
  expect(tabs?.active).toBe("won");
});

test("a rounding tie goes to the first tab, and a loss rounds away from zero", () => {
  const bets = [closed("won", NOW, 10.4), closed("sold", NOW - 1, 0.4)];
  expect(betTabs({ bets, betTotals: totals({ won: { n: 2, pnl: 10.4 }, sold: { n: 2, pnl: 0.4 } }) })?.tabs.map((tab) => tab.shown)).toEqual([11, 0]);
  expect(betTabs({ bets: [closed("lost", NOW, -2.5)], betTotals: totals({ lost: { n: 1, pnl: -2.5 } }) })?.tabs.map((tab) => tab.shown)).toEqual([-3]);
});

test("open bets lead the tabs and run soonest first, by kickoff, then end date, then neither", () => {
  const bets = [
    bet({ id: "kickoff later", kickoff: NOW + 300, endDate: NOW + 100 }),
    bet({ id: "end date", endDate: NOW + 200 }),
    bet({ id: "neither" }),
    bet({ id: "kickoff sooner", kickoff: NOW + 50 }),
    closed("lost", NOW, -390),
  ];
  const tabs = betTabs({ bets, betTotals: totals({ open: { n: 4, pnl: -1560 }, lost: { n: 1, pnl: -390 } }) });
  expect(tabs?.active).toBe("open");
  expect(tabs?.tabs[0]?.bets.map((b) => b.id)).toEqual(["kickoff sooner", "end date", "kickoff later", "neither"]);
});

test("with nothing open the newest closed bet picks the tab, and no bets make no tabs", () => {
  const bets = [closed("lost", NOW, -10), closed("won", NOW - 1, 30)];
  expect(betTabs({ bets, betTotals: totals({ won: { n: 1, pnl: 30 }, lost: { n: 1, pnl: -10 } }) })?.active).toBe("lost");
  expect(betTabs({ bets: [], betTotals: totals({}) })).toBeNull();
});

test("best and worst come as a pair from three closed bets, ties to the longer shot", () => {
  const bets = [
    bet({ id: "open", pnl: 900 }),
    closed("won", NOW, 20.2, { id: "won at 60%", entry: 0.6 }),
    closed("won", NOW, 19.8, { id: "won at 40%", entry: 0.4 }),
    closed("lost", NOW, -10, { id: "lost at 30%", entry: 0.3 }),
    closed("lost", NOW, -10.4, { id: "lost at 70%", entry: 0.7 }),
  ];
  const pair = highlights(bets);
  expect([pair?.best.id, pair?.worst.id]).toEqual(["won at 40%", "lost at 70%"]);
  expect(highlights(bets.slice(0, 3))).toBeNull();
  expect(highlights([closed("won", NOW, 5), closed("won", NOW, 6), closed("won", NOW, 7)])).toBeNull();
  expect(highlights([closed("won", NOW, 5), closed("lost", NOW, -0.4), closed("lost", NOW, -6)])?.worst.pnl).toBe(-6);
});

test("a bet is quiet under 2% of its stake or $5, whichever is more", () => {
  expect([4.99, -4.99, 5, -5].map((pnl) => quiet({ pnl, cost: 100 }))).toEqual([true, true, false, false]);
  expect([19.99, 20].map((pnl) => quiet({ pnl, cost: 1000 }))).toEqual([true, false]);
});

test("money tones round to the dollar, Today is the change since the last close", () => {
  expect([0.49, 0.5, -0.5, -0.49].map(moneyTone)).toEqual(["flat", "up", "down", "flat"]);
  expect([today(money(12, [])), today(money(12, [5])), today(money(12, [5, 7, 12]))]).toEqual([12, 12, 5]);
});

test("a list shows all of it unless that hides three or more", () => {
  expect([visible(10, 8), visible(11, 8), visible(3, 8)]).toEqual([10, 8, 3]);
});

test("the stake line reads the stake, the price and what became of it", () => {
  const sold = closed("sold", NOW, 10, { exit: 0.34 });
  expect([bet(), bet({ exit: null }), sold, closed("won", NOW, 10, { entry: 0.52, cost: 1962 })].map(stakeLine)).toEqual([
    { stake: "$1,500 on", price: "at 50%, now 37%" },
    { stake: "$1,500 on", price: "at 50%" },
    { stake: "$1,500 on", price: "at 50%, sold at 34%" },
    { stake: "$1,962 on", price: "at 52%" },
  ]);
});

test("a row caption reads the stage while open, the close time today and the day before", () => {
  expect(rowCaption(bet({ kickoff: NOW - 600, trading: true }), NOW)).toEqual({ text: "Live", live: true });
  expect(rowCaption(bet({ kickoff: NOW + 47 * 60 }), NOW)).toEqual({ text: "Starts in 47m", live: false });
  expect(rowCaption(bet(), NOW)).toEqual({ text: "", live: false });
  expect(rowCaption(closed("won", Date.UTC(2026, 9, 22, 5, 17) / 1000, 1), NOW)).toEqual({ text: "05:17", live: false });
  expect(rowCaption(closed("won", Date.UTC(2026, 9, 21, 23, 59) / 1000, 1), NOW)).toEqual({ text: "Oct 21", live: false });
});

test("the modal caption names a distinct event, then the stage or the close day", () => {
  expect(betCaption(bet({ eventTitle: "Rain this week", kickoff: NOW - 600, trading: true }), NOW)).toEqual({ event: "Rain this week", text: "Live", live: true });
  expect(betCaption(closed("sold", NOW, 1, { eventTitle: "Will it rain in Kyiv?" }), NOW)).toEqual({ event: null, text: "Sold Oct 22", live: false });
  expect(betCaption(closed("won", NOW - 86_400, 1, { eventTitle: "Rain on ___" }), NOW)).toEqual({ event: null, text: "Settled Oct 21", live: false });
  expect(betCaption(bet({ eventTitle: "Rain this week", endDate: NOW + 5 * 86_400 }), NOW)).toEqual({ event: "Rain this week", text: "Closes in 5d", live: false });
  expect(utc(NOW, NOW)).toBe("Oct 22, 11:12 UTC");
});

const ledger = (bets: readonly Bet[], change: Partial<Profile> = {}): Pick<Profile, "firstTradeAt" | "valuedAt" | "money" | "bets" | "betTotals"> => {
  const done = bets.filter((b) => b.status !== "open");
  return {
    firstTradeAt: NOW - 100 * HOUR,
    valuedAt: NOW,
    money: money(done.reduce((sum, b) => sum + b.pnl, 0)),
    bets,
    betTotals: totals({ won: { n: done.length, pnl: done.reduce((sum, b) => sum + b.pnl, 0) } }),
    ...change,
  };
};

test("Earned starts at $0, adds up each closed bet oldest first and ends at Earned now", () => {
  const series = earnedSeries(ledger([closed("won", NOW - 10 * HOUR, 30), closed("lost", NOW - 50 * HOUR, -10)]));
  expect(series).toEqual([
    { at: NOW - 100 * HOUR, value: 0 },
    { at: NOW - 50 * HOUR, value: -10 },
    { at: NOW - 10 * HOUR, value: 20 },
    { at: NOW, value: 20 },
  ]);
});

test("closes collapse to the last one in each of 40 slots", () => {
  const bets = Array.from({ length: 100 }, (_, i) => closed("won", NOW - (i + 1) * HOUR + 1, 1));
  const series = earnedSeries(ledger(bets, { firstTradeAt: NOW - 100 * HOUR }));
  expect(series.length).toBeLessThanOrEqual(42);
  expect([series[0], series.at(-1)]).toEqual([
    { at: NOW - 100 * HOUR, value: 0 },
    { at: NOW, value: 100 },
  ]);
});

test("a close at the valuation or past it folds into the end point, one before the first trade into the start", () => {
  expect(earnedSeries(ledger([closed("won", NOW + 60, 5), closed("won", NOW, 5)]))).toEqual([
    { at: NOW - 100 * HOUR, value: 0 },
    { at: NOW, value: 10 },
  ]);
  expect(earnedSeries(ledger([closed("won", NOW - 101 * HOUR, 5)]))).toEqual([
    { at: NOW - 100 * HOUR, value: 5 },
    { at: NOW, value: 5 },
  ]);
});

test("bets past the newest 100 closed start the line at their sum", () => {
  const series = earnedSeries(ledger([closed("won", NOW - 10 * HOUR, 30)], { betTotals: totals({ won: { n: 101, pnl: 100 } }), money: money(100) }));
  expect(series.map((point) => point.value)).toEqual([0, 100, 100]);
});

test("with nothing closed the line runs through the daily closes", () => {
  const series = earnedSeries(ledger([bet()], { firstTradeAt: Date.UTC(2026, 9, 20, 10) / 1000, money: money(-390, [0, 5, -390], "2026-10-20"), betTotals: totals({ open: { n: 1, pnl: -390 } }) }));
  expect(series).toEqual([
    { at: Date.UTC(2026, 9, 20, 10) / 1000, value: 0 },
    { at: Date.UTC(2026, 9, 21) / 1000, value: 0 },
    { at: Date.UTC(2026, 9, 22) / 1000, value: 5 },
    { at: NOW, value: -390 },
  ]);
});

test("the Earned chart spans the box, centres a flat line and fills only above three points", () => {
  const chart = earnedChart(ledger([closed("won", NOW - 10 * HOUR, 30), closed("lost", NOW - 50 * HOUR, -10)]));
  expect(chart.line).toBe("M0,640L500,920L900,80L1000,80");
  expect([chart.zero, chart.fill, chart.area.endsWith("V640H0Z")]).toEqual([640, true, true]);
  expect(chart.label).toBe("Earned from $0 on Oct 18 to +$20 now, 2 closed bets added up");
  const flat = earnedChart(ledger([], { money: money(0, [0, 0]), betTotals: totals({}) }));
  expect([flat.points.map((point) => point.y), flat.zero, flat.fill]).toEqual([[500, 500], 500, false]);
  expect(flat.label).toBe("Earned from $0 on Oct 18 to $0 now, daily closes");
});

test("a return reads in whole percent from 10%, with one decimal below and near a total loss", () => {
  expect([0.92, -0.998, 0, 0.0004, 0.0994, 0.0995, 12.345, -1].map(percent)).toEqual(["+92%", "−99.8%", "0%", "0%", "+9.9%", "+10%", "+1,235%", "−100%"]);
});

test("a holding time reads in days and hours, hours and minutes, or minutes", () => {
  expect([0, 59, 60, 3600, 28_200, 86_400, 90_000, 89_940].map(lasted)).toEqual(["Under a minute", "Under a minute", "1m", "1h", "7h 50m", "1d", "1d 1h", "1d"]);
});

test("an open bet's facts read its worth, its payout and its timing", () => {
  expect(facts(bet({ endDate: NOW + 5 * 86_400 }), NOW)).toEqual([
    ["Put in", "$1,500.00"],
    ["Worth now", "$1,110.00"],
    ["Average price", "50%"],
    ["Now", "37%"],
    ["Shares", "3,000"],
    ["Pays if right", "$3,000.00"],
    ["Opened", "Oct 22, 03:22 UTC"],
    ["Closes", "Oct 27, 11:12 UTC"],
    ["Held for", "7h 50m"],
  ]);
  expect(facts(bet({ exit: null, kickoff: NOW - 600 }), NOW).filter(([term]) => term === "Now" || term === "Started")).toEqual([
    ["Now", "No book"],
    ["Started", "Oct 22, 11:02 UTC"],
  ]);
  expect(facts(bet({ endDate: Date.UTC(2028, 0, 1, 4, 59) / 1000 }), NOW).find(([term]) => term === "Closes")).toEqual(["Closes", "Jan 1, 2028, 04:59 UTC"]);
});

const opened = Date.UTC(2026, 9, 21, 4, 40) / 1000;
const settled = Date.UTC(2026, 9, 22, 5, 17) / 1000;

test("a won bet's facts read what came back and how it resolved", () => {
  const won = closed("won", settled, 1811, { openedAt: opened, entry: 0.52, shares: 3773, held: 0, cost: 1962, back: 3773, winner: "Yes" });
  expect(facts(won, NOW)).toEqual([
    ["Put in", "$1,962.00"],
    ["Got back", "$3,773.00"],
    ["Average price", "52%"],
    ["Settled at", "100%"],
    ["Shares", "3,773"],
    ["Held for", "1d"],
    ["Opened", "Oct 21, 04:40 UTC"],
    ["Closed", "Oct 22, 05:17 UTC"],
    ["Result", "Resolved Yes"],
  ]);
});

test("a partial sell before the result reads the early sale and where the rest settled", () => {
  const fills = [fill(settled, "REDEEM", 600, null, 600), fill(opened + HOUR, "SELL", 400, 0.6, 240), fill(opened, "BUY", 1000, 0.4, 400)];
  const won = closed("won", settled, 440, { openedAt: opened, entry: 0.4, shares: 1000, sold: 400, sells: 1, cost: 400, back: 840, winner: "Lakers", fills });
  expect(facts(won, NOW)).toEqual([
    ["Put in", "$400.00"],
    ["Got back", "$840.00"],
    ["Average price", "40%"],
    ["Rest settled at", "100% on 600"],
    ["Shares", "1,000"],
    ["Sold early", "400 for $240.00"],
    ["Opened", "Oct 21, 04:40 UTC"],
    ["Closed", "Oct 22, 05:17 UTC"],
    ["Held for", "1d"],
    ["Result", "Lakers won"],
  ]);
  expect(fillCounts(won)).toEqual(["1 buy", "1 sell", "1 payout"]);
  expect(won.fills.map(fillLine)).toEqual(["Paid out", "Sold 400 at 60%", "Bought 1,000 at 40%"]);
});

test("a sold bet reads its sell price, averaged over two sells, and a split its 50-50 result", () => {
  const sold = closed("sold", settled, -170, { openedAt: opened, exit: 0.34, sold: 3000, sells: 1, back: 1020 });
  const pick = (b: Bet) => facts(b, NOW).filter(([term]) => !["Put in", "Average price", "Shares", "Opened", "Closed"].includes(term));
  expect(pick(sold)).toEqual([
    ["Got back", "$1,020.00"],
    ["Sold at", "34%"],
    ["Held for", "1d"],
  ]);
  expect(pick({ ...sold, sells: 2 })[1]).toEqual(["Average sell price", "34%"]);
  expect(pick(closed("split", settled, 0, { openedAt: opened, back: 1500 }))).toEqual([
    ["Got back", "$1,500.00"],
    ["Settled at", "50%"],
    ["Held for", "1d"],
    ["Result", "Resolved 50-50"],
  ]);
  expect(fillCounts(bet({ buys: 2 }))).toEqual(["2 buys"]);
});

test("the thin line shows only for an open bet that sells for under 90% of its worth", () => {
  expect([bet({ value: 1000, sellsFor: 850 }), bet({ value: 1000, sellsFor: 900 }), bet({ value: 5.4, sellsFor: 4.6 }), closed("won", NOW, 1, { value: 1000, sellsFor: 0 })].map(thinLine)).toEqual([
    "Worth $1,000, sells for $850 now",
    null,
    null,
    null,
  ]);
});

test("the price chart merges nearby fills of one side into one dot and nudges close labels apart", () => {
  const start = NOW - 10 * HOUR;
  const fills = [fill(start + 2 * HOUR, "BUY", 100, 0.5, 50), fill(start + 1000, "BUY", 100, 0.5, 50), fill(start, "BUY", 100, 0.5, 50)];
  const chart = priceChart(
    bet({
      openedAt: start,
      buys: 3,
      entry: 0.5,
      exit: 0.52,
      fills,
      prices: [
        { at: start - HOUR, price: 0.45 },
        { at: start + 5 * HOUR, price: 0.6 },
        { at: NOW + 60, price: 0.9 },
      ],
    }),
    NOW,
  );
  expect(chart.left).toBe("Oct 22");
  expect(chart.dots).toEqual([
    { x: 11.62, y: 50, side: "BUY" },
    { x: 27.27, y: 50, side: "BUY" },
  ]);
  expect(chart.line).toBe("M0,544L116.2,500L272.7,500L545.5,412L1000,482.4");
  expect([chart.entry, chart.end, chart.right, chart.legend, chart.soldAvg]).toEqual([{ x: 11.62, y: 50 }, 48.24, "Now", false, null]);
  expect(chart.tags).toEqual([
    { y: 43.12, text: "now 52%", kind: "exit" },
    { y: 55.12, text: "avg buy 50%", kind: "entry" },
    { y: 6, text: "100%", kind: "tick" },
    { y: 94, text: "0%", kind: "tick" },
  ]);
  expect(chart.label).toBe("Yes price from 45% on Oct 22 to 52% now, bought at 50% on average");
  expect(priceChart(bet({ entry: 0.97, exit: 0.99 }), NOW).tags.map(({ y, kind }) => [kind, y])).toEqual([
    ["exit", 3],
    ["entry", 15],
    ["tick", 94],
  ]);
});

test("a sold bet's chart ends at its last sell with no end dot, two sells add the dashed average", () => {
  const start = NOW - 10 * HOUR;
  const fills = [fill(start + 8 * HOUR, "SELL", 50, 0.7, 35), fill(start + 4 * HOUR, "SELL", 50, 0.6, 30), fill(start, "BUY", 100, 0.5, 50)];
  const chart = priceChart(closed("sold", start + 8 * HOUR, 15, { openedAt: start, entry: 0.5, exit: 0.65, sells: 2, fills }), NOW);
  expect([chart.end, chart.right, chart.legend, chart.soldAvg]).toEqual([null, "Sold", true, { x: 50, y: 36.8 }]);
  expect(chart.dots.map((dot) => [dot.side, dot.x])).toEqual([
    ["BUY", 0],
    ["SELL", 50],
    ["SELL", 100],
  ]);
  expect(chart.tags[0]).toEqual({ y: 36.8, text: "avg sold 65%", kind: "exit" });
  expect(priceChart(closed("won", NOW, 10), NOW).right).toBe("Settled");
});
