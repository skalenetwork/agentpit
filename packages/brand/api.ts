import { etDay, until } from "./format";

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
  readonly kickoff: number | null;
  readonly trading: boolean | null;
}

interface OpenPosition extends MarketContext {
  readonly title: string;
  readonly icon: string | null;
  readonly category: string | null;
  readonly outcome: string;
  readonly shares: number;
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
  readonly tx: string | null;
}

export interface Profile {
  readonly name: string;
  readonly address: string;
  readonly owner: string | null;
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

export type Phase = { readonly kind: "starts" | "closes"; readonly at: number } | { readonly kind: "live" } | { readonly kind: "awaiting" } | { readonly kind: "settled"; readonly at: number | null };

export type Timing = Pick<WireCard, "state" | "startTime" | "endDate" | "resolvedAt">;

export const phase = ({ state, startTime, endDate, resolvedAt }: Timing, live: boolean | null, now: number): Phase | null =>
  state === "settled"
    ? { kind: "settled", at: resolvedAt }
    : startTime !== null && now < startTime
      ? { kind: "starts", at: startTime }
      : startTime !== null && live !== null
        ? { kind: live ? "live" : "awaiting" }
        : endDate === null
          ? null
          : now < endDate
            ? { kind: "closes", at: endDate }
            : { kind: "awaiting" };

const WEEK = 7 * 86_400;

export const phaseText = (p: Phase, now: number): readonly [string, string?] => {
  if (p.kind === "starts" || p.kind === "closes") {
    const verb = p.kind === "starts" ? "Starts" : "Closes";
    return p.at - now < WEEK ? [`${verb} in`, until(p.at - now)] : [`${verb} ${etDay(p.at, now)}`];
  }
  return [p.kind === "live" ? "Live" : p.kind === "awaiting" ? "Awaiting result" : p.at === null ? "Settled" : `Settled ${etDay(p.at, now)}`];
};

export const marketCaption = ({ eventTitle, endDate, resolvedAt, winner, kickoff, trading }: MarketContext, now: number): readonly string[] => {
  const result = winner !== null ? `${winner} won` : resolvedAt !== null ? "Split 50-50" : null;
  const p = phase({ state: result === null ? "live" : "settled", startTime: kickoff, endDate, resolvedAt: resolvedAt ?? endDate }, trading, now);
  const status = p === null ? [] : [phaseText(p, now).join(" "), ...(result === null ? [] : [result])];
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

export interface WireEntry {
  readonly rank: number | null;
  readonly rankChange: number | null;
  readonly name: string;
  readonly address: string;
  readonly runner: Runner;
  readonly earned: string;
  readonly returnPct: number;
  readonly trades: number;
  readonly tradesToday: number;
  readonly trend: readonly string[];
  readonly firstTradeAt: number;
  readonly lastTradeAt: number;
}

export interface Agent extends Omit<WireEntry, "earned" | "trend"> {
  readonly pnl: number;
  readonly trend: readonly number[];
}

export const entry = ({ earned, trend, ...rest }: WireEntry): Agent => ({ ...rest, pnl: usd(earned), trend: trend.map(usd) });

export interface OwnerPage {
  readonly name: string;
  readonly trades: number;
  readonly firstTradeAt: number;
  readonly lastTradeAt: number;
  readonly money: { readonly returnPct: number; readonly earned: number };
  readonly agents: readonly Agent[];
}

export interface WireOwnerPage extends Omit<OwnerPage, "money" | "agents"> {
  readonly money: { readonly returnPct: number; readonly earned: string };
  readonly agents: readonly WireEntry[];
}

export const ownerPage = ({ money, agents, ...rest }: WireOwnerPage): OwnerPage => ({
  ...rest,
  money: { ...money, earned: usd(money.earned) },
  agents: agents.map(entry),
});

export interface Prize {
  readonly usd: number;
  readonly skl: number;
}

export interface RaceHead {
  readonly slug: string;
  readonly title: string;
  readonly startsAt: number;
  readonly endsAt: number;
  readonly phase: "upcoming" | "locked" | "live" | "ended";
  readonly final: boolean;
  readonly prizes: readonly Prize[];
  readonly entrants: number;
  readonly ranked: number;
  readonly line: string;
}

export interface WireScore {
  readonly rank: number | null;
  readonly earned: string;
  readonly returnPct: number;
}

export interface Score extends Omit<WireScore, "earned"> {
  readonly pnl: number;
}

export interface WireStanding {
  readonly address: string;
  readonly name: string;
  readonly runner: Runner;
  readonly listed: boolean;
  readonly runStartsAt: number;
  readonly endAt: number | null;
  readonly trades: number;
  readonly lastTradeAt: number | null;
  readonly disqualified: boolean;
  readonly rank: number | null;
  readonly earned: string;
  readonly returnPct: number;
  readonly rankChange: number | null;
  readonly behind: string | null;
  readonly wouldRank: number | null;
  readonly hoursLed: number;
  readonly bids: WireScore | null;
  readonly bell: WireScore | null;
}

export interface Standing extends Omit<WireStanding, "earned" | "behind" | "bids" | "bell"> {
  readonly pnl: number;
  readonly behind: number | null;
  readonly bids: Score | null;
  readonly bell: Score | null;
}

export interface Series {
  readonly at: readonly number[];
  readonly lines: readonly { readonly address: string; readonly ranks: readonly (number | null)[] }[];
}

export interface WireStake {
  readonly address: string;
  readonly name: string;
  readonly outcome: number;
  readonly shares: number;
  readonly value: string;
  readonly places: readonly (number | null)[];
}

export interface Stake extends Omit<WireStake, "value"> {
  readonly value: number;
}

export interface WireFixture extends MarketContext {
  readonly market: string;
  readonly question: string;
  readonly url: string | null;
  readonly outcomes: readonly string[];
  readonly stakes: readonly WireStake[];
}

export interface Fixture extends Omit<WireFixture, "stakes"> {
  readonly stakes: readonly Stake[];
}

export interface WireTapeFill {
  readonly at: number;
  readonly address: string;
  readonly name: string;
  readonly kind: "buy" | "sell" | "payout";
  readonly market: string;
  readonly question: string;
  readonly outcome: string | null;
  readonly price: number | null;
  readonly usd: string;
  readonly fills: number;
}

export interface TapeFill extends Omit<WireTapeFill, "usd"> {
  readonly usd: number;
}

interface SummaryOf<S> extends RaceHead {
  readonly standings: readonly S[];
}

interface RaceOf<S, F, T> extends SummaryOf<S> {
  readonly builtAt: number;
  readonly leadChanges: number;
  readonly series: Series;
  readonly fixtures: readonly F[];
  readonly tape: readonly T[];
}

interface EntryOf<S> {
  readonly race: RaceHead;
  readonly standing: S;
  readonly above: S | null;
  readonly below: S | null;
  readonly line: string;
}

export type WireRaceSummary = SummaryOf<WireStanding>;
export type RaceSummary = SummaryOf<Standing>;
export type WireRace = RaceOf<WireStanding, WireFixture, WireTapeFill>;
export type Race = RaceOf<Standing, Fixture, TapeFill>;
export type WireRaceEntry = EntryOf<WireStanding>;
export type RaceEntry = EntryOf<Standing>;

const score = ({ earned, ...rest }: WireScore): Score => ({ ...rest, pnl: usd(earned) });

const standing = ({ earned, behind, bids, bell, ...rest }: WireStanding): Standing => ({
  ...rest,
  pnl: usd(earned),
  behind: behind === null ? null : usd(behind),
  bids: bids && score(bids),
  bell: bell && score(bell),
});

export const raceSummary = ({ standings, ...rest }: WireRaceSummary): RaceSummary => ({ ...rest, standings: standings.map(standing) });

export const race = ({ standings, fixtures, tape, ...rest }: WireRace): Race => ({
  ...rest,
  standings: standings.map(standing),
  fixtures: fixtures.map(({ stakes, ...fixture }) => ({ ...fixture, stakes: stakes.map(({ value, ...stake }) => ({ ...stake, value: usd(value) })) })),
  tape: tape.map((fill) => ({ ...fill, usd: usd(fill.usd) })),
});

export const raceEntries = ({ entries }: { readonly entries: readonly WireRaceEntry[] }): readonly RaceEntry[] =>
  entries.map(({ standing: own, above, below, ...rest }) => ({ ...rest, standing: standing(own), above: above && standing(above), below: below && standing(below) }));

export const stageText = ({ phase, startsAt, endsAt }: RaceHead, now: number) =>
  phase === "upcoming" || phase === "locked"
    ? `Starts in ${until(Math.max(0, startsAt - now))}`
    : phase === "live"
      ? `Ends in ${until(Math.max(0, endsAt - now))}`
      : "Ended";

export const EXPLORER = "https://skale-base-explorer.skalenodes.com";
export const explorerTx = (hash: string) => `${EXPLORER}/tx/${hash}`;
export const explorerAddress = (address: string) => `${EXPLORER}/address/${address}?tab=token_transfers`;

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

export interface Team {
  readonly logo: string | null;
  readonly record: string | null;
  readonly color: string | null;
  readonly abbr: string | null;
}

interface WireOutcome {
  readonly label: string;
  readonly question: string;
  readonly slug: string;
  readonly url: string | null;
  readonly market: number;
  readonly price: number | null;
  readonly ask: number | null;
  readonly change24h: number | null;
  readonly team: Team | null;
  readonly bets: readonly WireBet[];
}

export interface WireCard {
  readonly slug: string;
  readonly title: string;
  readonly icon: string | null;
  readonly category: string | null;
  readonly url: string | null;
  readonly volume: number | null;
  readonly kind: "binary" | "multi" | "matchup" | "window";
  readonly state: "live" | "settled";
  readonly startTime: number | null;
  readonly trading: boolean | null;
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
  readonly status: "upcoming" | "live" | "started" | "settled";
  readonly tz: string;
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
    const found = /\((BO\d)\) - /.exec(title);
    const rest = found ? title.slice(found.index + found[0].length) : "";
    return found && rest ? `${found[1]} · ${rest}` : null;
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
