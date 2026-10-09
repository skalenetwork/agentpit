import { expect, test } from "bun:test";
import { agentPrompt, board, gameStage, marketCaption, phase, phaseText, type WireBoard, type WireCard } from "./api";

const multi: WireCard = {
  slug: "fed-decision-in-october",
  title: "Fed decision in October?",
  icon: null,
  category: "Business",
  url: "https://polymarket.com/event/fed-decision-in-october",
  volume: 1200000,
  kind: "multi",
  state: "live",
  startTime: null,
  trading: null,
  endDate: 1793000000,
  resolvedAt: null,
  outcomeCount: 4,
  lead: 0,
  outcomes: [
    {
      label: "No change",
      question: "No change after October Fed meeting?",
      slug: "no-change-in-fed-interest-rates-after-october-2026-meeting",
      url: "https://polymarket.com/market/no-change-in-fed-interest-rates-after-october-2026-meeting",
      market: 1,
      price: 0.8,
      ask: 0.81,
      change24h: 0.04,
      team: null,
      bets: [{ agent: "0xa", name: "Alpha", side: "No", value: "100000000", avgPrice: 0.3, pnl: "-50000000" }],
    },
    {
      label: "25 bps decrease",
      question: "Fed decreases interest rates by 25 bps after October 2026 meeting?",
      slug: "fed-decreases-interest-rates-by-25-bps-after-october-2026-meeting",
      url: null,
      market: 2,
      price: 0.15,
      ask: 0.16,
      change24h: null,
      team: null,
      bets: [
        { agent: "0xb", name: "Beta", side: "Yes", value: "300000000", avgPrice: 0.1, pnl: "150000000" },
        { agent: "0xa", name: "Alpha", side: "Yes", value: "20000000", avgPrice: 0.2, pnl: "-5000000" },
      ],
    },
  ],
};

const wire: WireBoard = {
  asOf: 1791199781,
  liveMarkets: 1718,
  tab: "trending",
  tabs: [{ key: "trending", label: "Trending", count: 1, category: false }],
  q: null,
  page: 1,
  pages: 1,
  total: 1,
  cards: [multi],
  sports: {
    item: "upcoming",
    views: [{ key: "upcoming", label: "Upcoming", count: 1 }],
    sports: [],
    games: [{ ...multi, kind: "matchup", title: "Counter-Strike: Vitality vs NAVI (BO3) - ESL Pro League Group Stage", league: "cs2", leagueLabel: "Counter-Strike 2", sport: "esports", status: "upcoming", startTime: 1791205200, tz: "UTC" }],
    futures: [],
    settled: 0,
    noBook: {},
  },
};

test("a board card ranks every bet by value and sums the money", () => {
  const [card] = board(wire).cards;
  expect(card.bets.map((b) => [b.i, b.name, b.outcome, Math.round((b.now ?? 0) * 100)])).toEqual([
    [0, "Beta", "Yes on 25 bps decrease", 15],
    [1, "Alpha", "No on No change", 20],
    [2, "Alpha", "Yes on 25 bps decrease", 15],
  ]);
  expect(card.agents).toEqual(["0xb", "0xa"]);
  expect([card.money, card.pnl]).toEqual([420, 95]);
  expect(card.outcomes[1].bets.map((b) => b.value)).toEqual([300, 20]);
});

test("a game keeps its league and reads its stage from the title", () => {
  const [game] = board(wire).sports?.games ?? [];
  expect([game.league, game.status, game.stage, game.bets[0].outcome]).toEqual(["cs2", "upcoming", "BO3 · ESL Pro League Group Stage", "Yes"]);
  expect([gameStage("Tennis: Gauff vs Su", "tennis"), gameStage("Albania vs. San Marino", "soccer")]).toEqual(["Tennis", null]);
});

test("the agent prompt names the market of a binary card and the event otherwise", () => {
  expect(agentPrompt(multi)).toBe(
    'Read https://agentpit.dev/skill.md, then look at the Polymarket event "Fed decision in October?" (event slug fed-decision-in-october) on AgentPit and decide whether to trade any of its markets.',
  );
  expect(agentPrompt({ ...multi, kind: "binary" })).toBe(
    'Read https://agentpit.dev/skill.md, then look at the Polymarket market "No change after October Fed meeting?" (slug no-change-in-fed-interest-rates-after-october-2026-meeting) on AgentPit and decide whether to trade it.',
  );
});

test("a market caption names a multi-market event, then when it closes, that its game is live or how it settled", () => {
  const now = 1791199781;
  const open = { eventTitle: null, endDate: 1793000000, resolvedAt: null, winner: null, kickoff: null, trading: null };
  expect(marketCaption(open, now)).toEqual(["Closes Oct 26"]);
  expect(marketCaption({ ...open, eventTitle: "Fed decision in October?", endDate: 1791000000 }, now)).toEqual(["Fed decision in October?", "Awaiting result"]);
  expect(marketCaption({ ...open, endDate: null }, now)).toEqual([]);
  expect(marketCaption({ ...open, resolvedAt: 1791100000, winner: "Yes" }, now)).toEqual(["Settled Oct 4", "Yes won"]);
  expect(marketCaption({ ...open, endDate: 1791000000, winner: "Vitality" }, now)).toEqual(["Settled Oct 3", "Vitality won"]);
  expect(marketCaption({ ...open, resolvedAt: 1791100000 }, now)).toEqual(["Settled Oct 4", "Split 50-50"]);
  expect(marketCaption({ ...open, endDate: 1791000000, kickoff: 1791190000, trading: true }, now)).toEqual(["Live"]);
  expect(marketCaption({ ...open, endDate: 1791000000, kickoff: 1791190000, trading: false }, now)).toEqual(["Awaiting result"]);
  expect(marketCaption({ ...open, kickoff: 1791205200 }, now)).toEqual(["Starts in 1h 30m"]);
  expect(marketCaption({ ...open, endDate: now + 200 }, now)).toEqual(["Closes in 3:20"]);
  expect(marketCaption({ ...open, kickoff: 1791190000, resolvedAt: 1791100000, winner: "Vitality" }, now)).toEqual(["Settled Oct 4", "Vitality won"]);
});

test("a card phase starts a window, closes it, awaits its result and keeps a game live only while its book is two-sided", () => {
  const now = 1791199781;
  const window = { state: "live", startTime: now + 480, endDate: now + 780, resolvedAt: null } as const;
  expect([window, { ...window, startTime: now - 48 }, { ...window, startTime: now - 900, endDate: now - 600 }].map((card) => phase(card, null, now))).toEqual([
    { kind: "starts", at: now + 480 },
    { kind: "closes", at: now + 780 },
    { kind: "awaiting" },
  ]);
  const game = { ...window, startTime: now - 3600, endDate: null };
  expect([phase(game, true, now), phase(game, false, now), phase({ ...game, state: "settled", resolvedAt: now }, false, now)]).toEqual([{ kind: "live" }, { kind: "awaiting" }, { kind: "settled", at: now }]);
  expect([phaseText({ kind: "starts", at: now + 480 }, now), phaseText({ kind: "closes", at: now + 252 }, now), phaseText({ kind: "closes", at: now + 7 * 86_400 }, now)]).toEqual([
    ["Starts in", "8m"],
    ["Closes in", "4:12"],
    ["Closes Oct 12"],
  ]);
});
