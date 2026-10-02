import { bursts } from "@agentpit/brand/activity";
import { type Fill, profile, type Runner, usd, type WireProfile } from "@agentpit/brand/api";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type Me, open, request } from "./session";

interface WireAgent {
  readonly handle: string | null;
  readonly eth_address: string;
  readonly created_at: number;
  readonly runner: Runner;
  readonly equity: string;
  readonly earned: string;
  readonly return_pct: number;
  readonly trades: number;
  readonly last_trade_at: number | null;
  readonly place: number | null;
}

export interface Agent extends Omit<WireAgent, "handle" | "equity" | "earned"> {
  readonly name: string;
  readonly equity: number;
  readonly earned: number;
}

export interface Position {
  readonly asset: string;
  readonly icon: string;
  readonly title: string;
  readonly outcome: string;
  readonly avgPrice: number;
  readonly curPrice: number;
  readonly currentValue: number;
  readonly cashPnl: number;
  readonly settled: boolean;
}

interface WireActivity {
  readonly timestamp: number;
  readonly type: Fill["type"];
  readonly side: "BUY" | "SELL" | "";
  readonly outcome: string;
  readonly size: number;
  readonly usdcSize: number;
  readonly title: string;
  readonly icon: string;
}

export interface Order {
  readonly id: string;
  readonly title: string;
  readonly side: "BUY" | "SELL";
  readonly outcome: string;
  readonly price: string;
  readonly original_size: string;
  readonly created_at: number;
}

const AGENTS = ["agents"];

const agent = ({ handle, equity, earned, ...rest }: WireAgent): Agent => ({ ...rest, name: handle ?? rest.runner.label, equity: usd(equity), earned: usd(earned) });

const fill = (wire: WireActivity): Fill & { readonly icon: string } => ({
  icon: wire.icon,
  at: wire.timestamp,
  type: wire.type,
  side: wire.side || null,
  outcome: wire.outcome || null,
  shares: wire.size,
  dollars: wire.usdcSize,
  title: wire.title,
  category: null,
});

export const useMe = () => useQuery({ queryKey: ["me"], queryFn: () => request<Me>("/me"), staleTime: Number.POSITIVE_INFINITY });

export const useAgents = (watch = false) =>
  useQuery({
    queryKey: AGENTS,
    queryFn: async () => (await request<readonly WireAgent[]>("/me/agents")).map(agent),
    refetchInterval: (query) => (watch || query.state.data?.length === 0 ? 3000 : false),
  });

export const useProfile = ({ eth_address, trades }: Agent) =>
  useQuery({ queryKey: ["profile", eth_address], enabled: trades > 0, queryFn: async () => profile(await open<WireProfile>(`/agents/${eth_address}`)) });

export const usePositions = ({ eth_address, trades }: Agent) =>
  useQuery({
    queryKey: ["positions", eth_address],
    enabled: trades > 0,
    queryFn: async () => (await open<readonly Position[]>(`/positions?user=${eth_address}`)).filter((p) => !p.settled).sort((a, b) => b.currentValue - a.currentValue),
  });

export const useActivity = ({ eth_address, trades }: Agent) =>
  useQuery({
    queryKey: ["activity", eth_address],
    enabled: trades > 0,
    queryFn: async () => bursts((await open<readonly WireActivity[]>(`/activity?user=${eth_address}&limit=80`)).map(fill)),
  });

export const useOrders = ({ eth_address }: Agent) => useQuery({ queryKey: ["orders", eth_address], queryFn: () => request<readonly Order[]>(`/me/agents/${eth_address}/orders`) });

const useAgentMutation = <Input, Output>(run: (input: Input) => Promise<Output>) => {
  const client = useQueryClient();
  return useMutation({ mutationFn: run, onSuccess: () => client.invalidateQueries({ queryKey: AGENTS }), gcTime: 0 });
};

export const useCreateAgent = () => useAgentMutation(() => request<{ readonly eth_address: string; readonly api_key: string }>("/me/agents", "POST"));
export const useRenameAgent = (address: string) => useAgentMutation((handle: string) => request(`/me/agents/${address}`, "PATCH", { handle }));
export const useDeleteAgent = (address: string) => useAgentMutation(() => request(`/me/agents/${address}`, "DELETE"));
