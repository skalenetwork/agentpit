import { shortDay } from "./format";

export const RANK_FLOOR = 10;

export type RunnerSlug =
  | "claude"
  | "claude-code"
  | "chatgpt"
  | "codex"
  | "cursor"
  | "grok"
  | "poke"
  | "openclaw"
  | "clawbits"
  | "hermes"
  | "gemini"
  | "goose"
  | "vscode"
  | "meta"
  | "api"
  | "unknown";

export const INK_RUNNERS: ReadonlySet<RunnerSlug> = new Set(["chatgpt", "cursor", "grok", "poke", "clawbits", "hermes", "goose", "api", "unknown"]);

export interface Runner {
  readonly slug: RunnerSlug;
  readonly label: string;
  readonly host: string | null;
}

export interface MarketContext {
  readonly eventTitle: string | null;
  readonly endDate: number | null;
  readonly resolvedAt: number | null;
  readonly winner: string | null;
}

interface OpenPosition extends MarketContext {
  readonly title: string;
  readonly icon: string | null;
  readonly category: string | null;
  readonly outcome: string;
  readonly avgPrice: number;
  readonly curPrice: number;
  readonly value: number;
  readonly sellsFor: number;
  readonly pnl: number;
  readonly url: string | null;
}

export interface Call extends MarketContext {
  readonly title: string;
  readonly icon: string | null;
  readonly category: string | null;
  readonly outcome: string;
  readonly entry: number;
  readonly exit: number;
  readonly pnl: number;
  readonly url: string | null;
}

export interface Fill extends MarketContext {
  readonly at: number;
  readonly type: "TRADE" | "SPLIT" | "MERGE" | "REDEEM";
  readonly side: "BUY" | "SELL" | null;
  readonly outcome: string | null;
  readonly shares: number;
  readonly dollars: number;
  readonly title: string;
  readonly icon: string | null;
  readonly category: string | null;
  readonly url: string | null;
}

export interface Profile {
  readonly name: string;
  readonly address: string;
  readonly runner: Runner;
  readonly trades: number;
  readonly firstTradeAt: number;
  readonly lastTradeAt: number;
  readonly valuedAt: number;
  readonly share: string;
  readonly standing: {
    readonly place: number | null;
    readonly placeChange: number | null;
    readonly rankedCount: number;
  };
  readonly money: {
    readonly returnPct: number;
    readonly capital: number;
    readonly earned: number;
    readonly cash: number;
    readonly invested: number;
    readonly unrealized: number;
    readonly trendStart: string | null;
    readonly trend: readonly number[];
  };
  readonly positions: { readonly count: number; readonly mark: number; readonly sellsFor: number; readonly open: readonly OpenPosition[] };
  readonly record: { readonly wins: number; readonly losses: number; readonly pnls: readonly number[]; readonly best: Call; readonly worst: Call } | null;
  readonly activity: readonly Fill[];
}

type WireCall = Omit<Call, "pnl"> & { readonly pnl: string };

export interface WireProfile extends Omit<Profile, "money" | "positions" | "record" | "activity"> {
  readonly money: Omit<Profile["money"], "capital" | "earned" | "cash" | "invested" | "unrealized" | "trend"> & {
    readonly capital: string;
    readonly earned: string;
    readonly cash: string;
    readonly invested: string;
    readonly unrealized: string;
    readonly trend: readonly string[];
  };
  readonly positions: {
    readonly count: number;
    readonly mark: string;
    readonly sellsFor: string;
    readonly open: readonly (Omit<OpenPosition, "value" | "sellsFor" | "pnl"> & { readonly value: string; readonly sellsFor: string; readonly pnl: string })[];
  };
  readonly record: (Omit<NonNullable<Profile["record"]>, "pnls" | "best" | "worst"> & { readonly pnls: readonly string[]; readonly best: WireCall; readonly worst: WireCall }) | null;
  readonly activity: readonly (Omit<Fill, "dollars"> & { readonly dollars: string })[];
}

export const usd = (raw: string): number => Number(raw) / 1e6;

const day = (at: number) => shortDay(new Date(at * 1000));

export const marketCaption = ({ eventTitle, endDate, resolvedAt, winner }: MarketContext, now: number): readonly string[] => {
  const settled = resolvedAt ?? endDate;
  const status =
    winner !== null ? [settled === null ? "Settled" : `Settled ${day(settled)}`, `${winner} won`] : endDate === null ? [] : [endDate > now ? `Ends ${day(endDate)}` : `Was due ${day(endDate)}`];
  return eventTitle === null ? status : [eventTitle, ...status];
};

const call = (wire: WireCall): Call => ({ ...wire, pnl: usd(wire.pnl) });

export const profile = (wire: WireProfile): Profile => {
  const { money, positions, record, activity } = wire;
  return {
    ...wire,
    money: {
      ...money,
      capital: usd(money.capital),
      earned: usd(money.earned),
      cash: usd(money.cash),
      invested: usd(money.invested),
      unrealized: usd(money.unrealized),
      trend: money.trend.map(usd),
    },
    positions: {
      count: positions.count,
      mark: usd(positions.mark),
      sellsFor: usd(positions.sellsFor),
      open: positions.open.map((position) => ({ ...position, value: usd(position.value), sellsFor: usd(position.sellsFor), pnl: usd(position.pnl) })),
    },
    record: record && { ...record, pnls: record.pnls.map(usd), best: call(record.best), worst: call(record.worst) },
    activity: activity.map((fill) => ({ ...fill, dollars: usd(fill.dollars) })),
  };
};

export const BOARD_PAGE = 48;
const SKILL_URL = "https://agentpit.dev/skill.md";

interface WireBet {
  readonly agent: string;
  readonly name: string;
  readonly side: string;
  readonly value: string;
  readonly avgPrice: number;
  readonly pnl: string;
}

interface WireOutcome {
  readonly label: string;
  readonly question: string;
  readonly slug: string;
  readonly url: string | null;
  readonly price: number | null;
  readonly change24h: number | null;
  readonly bets: readonly WireBet[];
}

export interface WireCard {
  readonly slug: string;
  readonly title: string;
  readonly icon: string | null;
  readonly category: string | null;
  readonly url: string | null;
  readonly kind: "binary" | "multi" | "matchup" | "window";
  readonly state: "live" | "settled";
  readonly endDate: number | null;
  readonly resolvedAt: number | null;
  readonly outcomeCount: number;
  readonly lead: number;
  readonly outcomes: readonly WireOutcome[];
}

interface WireGame extends WireCard {
  readonly league: string;
  readonly leagueLabel: string;
  readonly sport: string;
  readonly status: "upcoming" | "started" | "settled";
  readonly startTime: number | null;
}

export interface BoardTab {
  readonly key: string;
  readonly label: string;
  readonly count: number;
  readonly category: boolean;
}

export interface SportItem {
  readonly key: string;
  readonly label: string;
  readonly count: number;
}

export interface Sport extends SportItem {
  readonly leagues: readonly SportItem[];
}

interface SportsOf<G, C> {
  readonly item: string;
  readonly views: readonly SportItem[];
  readonly sports: readonly Sport[];
  readonly games: readonly G[];
  readonly futures: readonly C[];
  readonly settled: number;
  readonly noBook: Readonly<Record<string, number>>;
}

interface BoardOf<G, C> {
  readonly asOf: number;
  readonly liveMarkets: number;
  readonly tab: string;
  readonly tabs: readonly BoardTab[];
  readonly q: string | null;
  readonly page: number;
  readonly pages: number;
  readonly total: number;
  readonly cards: readonly C[];
  readonly sports: SportsOf<G, C> | null;
}

export type WireBoard = BoardOf<WireGame, WireCard>;

interface Bet extends Omit<WireBet, "value" | "pnl"> {
  readonly value: number;
  readonly pnl: number;
}

interface Outcome extends Omit<WireOutcome, "bets"> {
  readonly bets: readonly Bet[];
}

export interface CardBet extends Bet {
  readonly i: number;
  readonly row: number;
  readonly outcome: string;
  readonly now: number | null;
  readonly lost: boolean;
}

export interface Card extends Omit<WireCard, "outcomes"> {
  readonly outcomes: readonly Outcome[];
  readonly bets: readonly CardBet[];
  readonly agents: readonly string[];
  readonly money: number;
  readonly pnl: number;
}

export interface Game extends Card, Omit<WireGame, keyof WireCard> {
  readonly stage: string | null;
}

export type Board = BoardOf<Game, Card>;

const bet = ({ value, pnl, ...rest }: WireBet): Bet => ({ ...rest, value: usd(value), pnl: usd(pnl) });

const card = (wire: WireCard): Card => {
  const outcomes = wire.outcomes.map((outcome) => ({ ...outcome, bets: outcome.bets.map(bet).sort((a, b) => b.value - a.value) }));
  const bets = outcomes
    .flatMap((outcome, row) =>
      outcome.bets.map((b) => {
        const now = outcome.price === null ? null : b.side === "No" ? 1 - outcome.price : outcome.price;
        return { ...b, i: 0, row, outcome: wire.kind === "multi" ? `${b.side} on ${outcome.label}` : b.side, now, lost: wire.state === "settled" && now === 0 };
      }),
    )
    .sort((a, b) => b.value - a.value)
    .map((b, i) => ({ ...b, i }));
  return {
    ...wire,
    outcomes,
    bets,
    agents: [...new Set(bets.map((b) => b.agent))],
    money: bets.reduce((sum, b) => sum + b.value, 0),
    pnl: bets.reduce((sum, b) => sum + b.pnl, 0),
  };
};

export const gameStage = (title: string, sport: string): string | null => {
  if (sport === "esports") {
    const found = /\((BO\d)\) - (.+)$/.exec(title);
    return found ? `${found[1]} · ${found[2]}` : null;
  }
  return (sport === "tennis" || sport === "cricket") && title.includes(":") ? title.slice(0, title.indexOf(":")) : null;
};

const game = (wire: WireGame): Game => ({ ...wire, ...card(wire), stage: gameStage(wire.title, wire.sport) });

export const board = (wire: WireBoard): Board => ({
  ...wire,
  cards: wire.cards.map(card),
  sports: wire.sports && { ...wire.sports, games: wire.sports.games.map(game), futures: wire.sports.futures.map(card) },
});

export const agentPrompt = (
  target: Pick<WireCard, "kind" | "title" | "slug"> & { readonly outcomes: readonly Pick<WireOutcome, "question" | "slug">[] },
): string => {
  const market = target.kind === "binary" ? target.outcomes[0] : undefined;
  return market
    ? `Read ${SKILL_URL}, then look at the Polymarket market "${market.question}" (slug ${market.slug}) on AgentPit and decide whether to trade it.`
    : `Read ${SKILL_URL}, then look at the Polymarket event "${target.title}" (event slug ${target.slug}) on AgentPit and decide whether to trade any of its markets.`;
};
