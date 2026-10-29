import { profile, type RaceHead, type Runner, raceEntries, usd, type WireProfile, type WireRaceEntry } from "@agentpit/brand/api";
import { type QueryKey, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { end, type Me, open, request } from "./session";

interface WireAgent {
  readonly handle: string | null;
  readonly eth_address: string;
  readonly runner: Runner;
  readonly equity: string;
  readonly earned: string;
  readonly return_pct: number;
  readonly trades: number;
  readonly markets: number;
  readonly last_trade_at: number | null;
  readonly place: number | null;
  readonly place_change: number | null;
  readonly trend: readonly string[];
  readonly competitions: readonly string[];
  readonly next_top_up_at: number;
}

export interface Agent extends Omit<WireAgent, "handle" | "equity" | "earned" | "trend"> {
  readonly name: string;
  readonly equity: number;
  readonly earned: number;
  readonly trend: readonly number[];
}

export interface NewAgent {
  readonly handle: string;
  readonly eth_address: string;
  readonly api_key: string;
}

export interface Order {
  readonly id: string;
  readonly title: string;
  readonly side: "BUY" | "SELL";
  readonly outcome: string;
  readonly price: string;
  readonly original_size: string;
}

const ME = ["me"];
const AGENTS = ["agents"];
const COMPETITIONS = ["competitions"];

const agent = ({ handle, equity, earned, trend, ...rest }: WireAgent): Agent => ({
  ...rest,
  name: handle ?? rest.runner.label,
  equity: usd(equity),
  earned: usd(earned),
  trend: trend.map(usd),
});

export const useMe = () => useQuery({ queryKey: ME, queryFn: () => request<Me>("/me"), staleTime: Number.POSITIVE_INFINITY, refetchInterval: false });

export const useDeleteAccount = () => useMutation({ mutationFn: () => request("/me", "DELETE"), onSuccess: end });

export const useOwner = () => {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (change: { readonly name?: string; readonly shown?: boolean }) => request<Me>("/me/owner", "PATCH", change),
    onSuccess: (me) => client.setQueryData(ME, me),
  });
};

export const useAgents = () => useQuery({ queryKey: AGENTS, queryFn: async () => (await request<readonly WireAgent[]>("/me/agents")).map(agent) });

export const useProfile = ({ eth_address, trades }: Agent) =>
  useQuery({ queryKey: ["profile", eth_address], enabled: trades > 0, queryFn: async () => profile(await open<WireProfile>(`/agents/${eth_address}`)) });

export const useOrders = ({ eth_address }: Agent) => useQuery({ queryKey: ["orders", eth_address], queryFn: () => request<readonly Order[]>(`/me/agents/${eth_address}/orders`) });

export const useCompetitions = () =>
  useQuery({ queryKey: COMPETITIONS, queryFn: () => open<{ readonly competitions: readonly RaceHead[] }>("/competitions"), staleTime: 30_000, refetchInterval: 30_000 });

export const useRaceEntries = (address: string) =>
  useQuery({
    queryKey: [...COMPETITIONS, address],
    queryFn: async () => raceEntries(await open<{ readonly entries: readonly WireRaceEntry[] }>(`/agents/${address}/competitions`)),
    staleTime: 30_000,
    refetchInterval: 30_000,
  });

const useAgentMutation = <Input, Output>(run: (input: Input) => Promise<Output>, keys: readonly QueryKey[] = [AGENTS]) => {
  const client = useQueryClient();
  return useMutation({ mutationFn: run, onSuccess: () => Promise.all(keys.map((queryKey) => client.invalidateQueries({ queryKey }))), gcTime: 0 });
};

export const useCreateAgent = () => {
  const client = useQueryClient();
  return useMutation({ mutationFn: () => request<NewAgent>("/me/agents", "POST"), onSuccess: () => void client.invalidateQueries({ queryKey: AGENTS }), gcTime: 0 });
};

export const useRenameAgent = (address: string) => useAgentMutation((handle: string) => request(`/me/agents/${address}`, "PATCH", { handle }));
export const useDeleteAgent = (address: string) => useAgentMutation(() => request(`/me/agents/${address}`, "DELETE"));
export const useTopUp = (address: string) =>
  useAgentMutation(async () => usd((await request<{ readonly minted: string }>(`/me/agents/${address}/top-up`, "POST")).minted), [AGENTS, ["profile", address]]);
export const useEntry = (address: string) =>
  useAgentMutation(({ slug, enter }: { readonly slug: string; readonly enter: boolean }) => request(`/me/agents/${address}/competitions/${slug}`, enter ? "POST" : "DELETE"), [AGENTS, COMPETITIONS]);
