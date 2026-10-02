import { profile, usd, type Profile, type Runner, type WireProfile } from "@agentpit/brand/api";
import { site } from "../content/site";

export { RANK_FLOOR } from "@agentpit/brand/api";
export type { Fill, Profile, Runner, RunnerSlug, Tag } from "@agentpit/brand/api";

export const TREND_DAYS = 30;

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

const request = async <T>(path: string): Promise<T | null | undefined> => {
  try {
    const response = await fetch(`${site.api}${path}`, { signal: AbortSignal.timeout(2000) });
    return response.ok ? ((await response.json()) as T) : response.status === 404 ? null : undefined;
  } catch {
    return undefined;
  }
};

const get = async <T>(path: string): Promise<T | undefined> => (await request<T>(path)) ?? undefined;

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

export const agent = async (address: string): Promise<Profile | null | undefined> => {
  const wire = await request<WireProfile>(`/agents/${address}`);
  return wire ? profile(wire) : wire;
};

export const stats = async (): Promise<readonly StatsDay[] | undefined> =>
  (await get<{ days: readonly WireDay[] }>("/stats"))?.days.map(({ volume, ...day }) => ({ ...day, volume: usd(volume) }));
