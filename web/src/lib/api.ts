import { site } from "../content/site";

export const RANK_FLOOR = 10;

export interface Agent {
  readonly rank: number;
  readonly name: string;
  readonly address: string;
  readonly returnPct: number;
  readonly trades: number;
  readonly equity: number;
  readonly pnl: number;
  readonly invested: number;
  readonly trend: readonly number[];
}

export interface Point {
  readonly t: number;
  readonly returnPct: number;
}

interface Entry {
  readonly rank: number;
  readonly name: string;
  readonly address: string;
  readonly capital: string;
  readonly earned: string;
  readonly invested: string;
  readonly returnPct: number;
  readonly trades: number;
  readonly trend?: readonly number[];
}

const get = async <T>(path: string): Promise<T | undefined> => {
  try {
    const response = await fetch(`${site.api}${path}`, { signal: AbortSignal.timeout(2000) });
    return response.ok ? ((await response.json()) as T) : undefined;
  } catch {
    return undefined;
  }
};

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
    returnPct: entry.returnPct,
    trades: entry.trades,
    equity: usd(entry.capital),
    pnl: usd(entry.earned),
    invested: usd(entry.invested),
    trend: entry.trend ?? [],
  }));

export const history = async (address: string): Promise<readonly Point[]> =>
  (await get<{ points: readonly Point[] }>(`/leaderboard/${address}/history`))?.points.map(({ t, returnPct }) => ({
    t,
    returnPct,
  })) ?? [];
