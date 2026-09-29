import { site } from "../content/site";

export const RANK_FLOOR = 10;
export const TREND_DAYS = 30;

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

export interface Runner {
  readonly slug: RunnerSlug;
  readonly label: string;
  readonly host: string | null;
}

export interface Agent {
  readonly rank: number;
  readonly name: string;
  readonly address: string;
  readonly runner: Runner;
  readonly returnPct: number;
  readonly trades: number;
  readonly equity: number;
  readonly pnl: number;
  readonly invested: number;
  readonly trend: readonly number[];
  readonly firstTradeAt: number;
  readonly lastTradeAt: number;
}

export type Tag = "busiest" | "closestBattle" | "hottestRookie";

export interface Neighbour {
  readonly place: number | null;
  readonly name: string;
  readonly address: string;
  readonly returnPct: number;
  readonly trades: number;
}

export interface OpenPosition {
  readonly title: string;
  readonly category: string | null;
  readonly outcome: string;
  readonly avgPrice: number;
  readonly curPrice: number;
  readonly value: number;
  readonly sellsFor: number;
  readonly pnl: number;
}

export interface Call {
  readonly title: string;
  readonly category: string | null;
  readonly outcome: string;
  readonly entry: number;
  readonly exit: number;
  readonly pnl: number;
}

export interface Fill {
  readonly at: number;
  readonly type: "TRADE" | "SPLIT" | "MERGE" | "REDEEM";
  readonly side: "BUY" | "SELL" | null;
  readonly outcome: string | null;
  readonly shares: number;
  readonly dollars: number;
  readonly title: string;
  readonly category: string | null;
}

export interface Profile {
  readonly name: string;
  readonly address: string;
  readonly runner: Runner;
  readonly trades: number;
  readonly firstTradeAt: number;
  readonly lastTradeAt: number;
  readonly valuedAt: number;
  readonly standing: {
    readonly place: number | null;
    readonly rankedCount: number;
    readonly warmingCount: number;
    readonly gap: number | null;
    readonly tags: readonly Tag[];
    readonly neighbours: readonly Neighbour[];
  };
  readonly money: {
    readonly returnPct: number;
    readonly capital: number;
    readonly deposited: number;
    readonly earned: number;
    readonly cash: number;
    readonly invested: number;
    readonly unrealized: number;
    readonly trendStart: string | null;
    readonly trend: readonly number[];
  };
  readonly positions: { readonly count: number; readonly mark: number; readonly sellsFor: number; readonly top: readonly OpenPosition[] };
  readonly book: {
    readonly mix: readonly { readonly category: string | null; readonly share: number; readonly count: number }[];
    readonly entryPrice: readonly { readonly yes: number; readonly no: number }[];
    readonly horizonDays: number | null;
  } | null;
  readonly record: { readonly wins: number; readonly losses: number; readonly pnls: readonly number[]; readonly best: Call; readonly worst: Call } | null;
  readonly activity: readonly Fill[];
}

export interface StatsDay {
  readonly day: string;
  readonly agents: number;
  readonly trades: number;
  readonly active: number;
  readonly volume: number;
}

interface WireDay extends Omit<StatsDay, "volume"> {
  readonly volume: string;
}

interface Entry {
  readonly rank: number;
  readonly name: string;
  readonly address: string;
  readonly runner: Runner;
  readonly capital: string;
  readonly earned: string;
  readonly invested: string;
  readonly returnPct: number;
  readonly trades: number;
  readonly trend: readonly string[];
  readonly firstTradeAt: number;
  readonly lastTradeAt: number;
}

type WireCall = Omit<Call, "pnl"> & { readonly pnl: string };

interface WireProfile extends Omit<Profile, "money" | "positions" | "book" | "record" | "activity"> {
  readonly money: Omit<Profile["money"], "capital" | "deposited" | "earned" | "cash" | "invested" | "unrealized" | "trend"> & {
    readonly capital: string;
    readonly deposited: string;
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
    readonly top: readonly (Omit<OpenPosition, "value" | "sellsFor" | "pnl"> & { readonly value: string; readonly sellsFor: string; readonly pnl: string })[];
  };
  readonly book: (Omit<NonNullable<Profile["book"]>, "entryPrice"> & { readonly entryPrice: readonly { readonly yes: string; readonly no: string }[] }) | null;
  readonly record: (Omit<NonNullable<Profile["record"]>, "pnls" | "best" | "worst"> & { readonly pnls: readonly string[]; readonly best: WireCall; readonly worst: WireCall }) | null;
  readonly activity: readonly (Omit<Fill, "dollars"> & { readonly dollars: string })[];
}

const request = async <T>(path: string): Promise<T | null | undefined> => {
  try {
    const response = await fetch(`${site.api}${path}`, { signal: AbortSignal.timeout(2000) });
    return response.ok ? ((await response.json()) as T) : response.status === 404 ? null : undefined;
  } catch {
    return undefined;
  }
};

const get = async <T>(path: string): Promise<T | undefined> => (await request<T>(path)) ?? undefined;

const usd = (raw: string): number => Number(raw) / 1e6;

export const activeMarkets = async (): Promise<number | undefined> => {
  const active = (await get<{ active?: unknown }>("/markets/stats"))?.active;
  return typeof active === "number" ? active : undefined;
};

export const leaderboard = async (): Promise<readonly Agent[] | undefined> =>
  (await get<{ entries: readonly Entry[] }>("/leaderboard"))?.entries.map((entry) => ({
    rank: entry.rank,
    name: entry.name,
    address: entry.address,
    runner: entry.runner,
    returnPct: entry.returnPct,
    trades: entry.trades,
    equity: usd(entry.capital),
    pnl: usd(entry.earned),
    invested: usd(entry.invested),
    trend: entry.trend.map(usd),
    firstTradeAt: entry.firstTradeAt,
    lastTradeAt: entry.lastTradeAt,
  }));

const call = (wire: WireCall): Call => ({ ...wire, pnl: usd(wire.pnl) });

export const agent = async (address: string): Promise<Profile | null | undefined> => {
  const wire = await request<WireProfile>(`/agents/${address}`);
  if (!wire) return wire;
  const { money, positions, book, record, activity } = wire;
  return {
    ...wire,
    money: {
      ...money,
      capital: usd(money.capital),
      deposited: usd(money.deposited),
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
      top: positions.top.map((position) => ({ ...position, value: usd(position.value), sellsFor: usd(position.sellsFor), pnl: usd(position.pnl) })),
    },
    book: book && { ...book, entryPrice: book.entryPrice.map(({ yes, no }) => ({ yes: usd(yes), no: usd(no) })) },
    record: record && { ...record, pnls: record.pnls.map(usd), best: call(record.best), worst: call(record.worst) },
    activity: activity.map((fill) => ({ ...fill, dollars: usd(fill.dollars) })),
  };
};

export const stats = async (): Promise<readonly StatsDay[] | undefined> =>
  (await get<{ days: readonly WireDay[] }>("/stats"))?.days.map(({ volume, ...day }) => ({ ...day, volume: usd(volume) }));
