import { board, entry, ownerPage, profile, race, raceSummary, usd, type Agent, type Board, type OwnerPage, type Profile, type Race, type RaceSummary, type WireBoard, type WireEntry, type WireOwnerPage, type WireProfile, type WireRace, type WireRaceSummary } from "@agentpit/brand/api";
import { site } from "../content/site";

export { RANK_FLOOR } from "@agentpit/brand/api";
export type { Agent, Bet, OwnerPage, Profile } from "@agentpit/brand/api";

export const TREND_DAYS = 30;

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

const request = async <T>(path: string): Promise<T | null | undefined> => {
  try {
    const response = await fetch(`${site.api}${path}`, { signal: AbortSignal.timeout(5000) });
    return response.ok ? ((await response.json()) as T) : response.status === 404 ? null : undefined;
  } catch {
    return undefined;
  }
};

const get = async <T>(path: string): Promise<T | undefined> => (await request<T>(path)) ?? undefined;

export const leaderboard = async (): Promise<readonly Agent[] | undefined> => (await get<{ entries: readonly WireEntry[] }>("/leaderboard"))?.entries.map(entry);

export const agent = async (address: string): Promise<Profile | null | undefined> => {
  const wire = await request<WireProfile>(`/agents/${address}`);
  return wire ? profile(wire) : wire;
};

export const owner = async (name: string): Promise<OwnerPage | null | undefined> => {
  const wire = await request<WireOwnerPage>(`/owners/${name}`);
  return wire ? ownerPage(wire) : wire;
};

export const competitions = async (): Promise<readonly RaceSummary[] | undefined> =>
  (await get<{ competitions: readonly WireRaceSummary[] }>("/competitions"))?.competitions.map(raceSummary);

export const competition = async (slug: string, agent?: string): Promise<Race | null | undefined> => {
  const wire = await request<WireRace>(`/competitions/${slug}${agent ? `?agent=${agent}` : ""}`);
  return wire ? race(wire) : wire;
};

export const stats = async (): Promise<readonly StatsDay[] | undefined> =>
  (await get<{ days: readonly WireDay[] }>("/stats"))?.days.map(({ volume, ...day }) => ({ ...day, volume: usd(volume) }));

interface BoardQuery {
  readonly tab: string;
  readonly sport?: string;
  readonly q: string | null;
  readonly page: number;
}

export const boardQuery = (url: URL): Pick<BoardQuery, "q" | "page"> => ({
  q: url.searchParams.get("q")?.trim() || null,
  page: Math.max(1, Number.parseInt(url.searchParams.get("page") ?? "", 10) || 1),
});

export const marketBoard = async ({ tab, sport, q, page }: BoardQuery): Promise<Board | null | undefined> => {
  const params = new URLSearchParams({ tab, ...(sport && { sport }), ...(q && { q }), ...(page > 1 && { page: String(page) }) });
  const wire = await request<WireBoard>(`/markets/board?${params}`);
  return wire ? board(wire) : wire;
};
