import { type MarketContext, profile, type Runner, usd, type WireProfile } from "@agentpit/brand/api";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type Me, open, request } from "./session";

interface WireAgent {
  readonly handle: string | null;
  readonly eth_address: string;
  readonly runner: Runner;
  readonly equity: string;
  readonly earned: string;
  readonly return_pct: number;
  readonly trades: number;
  readonly last_trade_at: number | null;
  readonly place: number | null;
  readonly place_change: number | null;
  readonly trend: readonly string[];
}

export interface Agent extends Omit<WireAgent, "handle" | "equity" | "earned" | "trend"> {
  readonly name: string;
  readonly equity: number;
  readonly earned: number;
  readonly trend: readonly number[];
}

export interface Order extends MarketContext {
  readonly id: string;
  readonly title: string;
  readonly side: "BUY" | "SELL";
  readonly outcome: string;
  readonly price: string;
  readonly original_size: string;
  readonly url: string | null;
}

const AGENTS = ["agents"];

const agent = ({ handle, equity, earned, trend, ...rest }: WireAgent): Agent => ({
  ...rest,
  name: handle ?? rest.runner.label,
  equity: usd(equity),
  earned: usd(earned),
  trend: trend.map(usd),
});

export const useMe = () => useQuery({ queryKey: ["me"], queryFn: () => request<Me>("/me"), staleTime: Number.POSITIVE_INFINITY, refetchInterval: false });

export const useAgents = () => useQuery({ queryKey: AGENTS, queryFn: async () => (await request<readonly WireAgent[]>("/me/agents")).map(agent) });

export const useProfile = ({ eth_address, trades }: Agent) =>
  useQuery({ queryKey: ["profile", eth_address], enabled: trades > 0, queryFn: async () => profile(await open<WireProfile>(`/agents/${eth_address}`)) });

export const useOrders = ({ eth_address }: Agent) => useQuery({ queryKey: ["orders", eth_address], queryFn: () => request<readonly Order[]>(`/me/agents/${eth_address}/orders`) });

const useAgentMutation = <Input, Output>(run: (input: Input) => Promise<Output>) => {
  const client = useQueryClient();
  return useMutation({ mutationFn: run, onSuccess: () => client.invalidateQueries({ queryKey: AGENTS }), gcTime: 0 });
};

export const useCreateAgent = () => useAgentMutation(() => request<{ readonly eth_address: string; readonly api_key: string }>("/me/agents", "POST"));
export const useRenameAgent = (address: string) => useAgentMutation((handle: string) => request(`/me/agents/${address}`, "PATCH", { handle }));
export const useDeleteAgent = (address: string) => useAgentMutation(() => request(`/me/agents/${address}`, "DELETE"));
