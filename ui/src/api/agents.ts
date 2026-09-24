import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";

export interface AgentSummary {
  handle: string | null;
  app: string;
  eth_address: string;
  created_at: number;
}

export function listMyAgents(): Promise<AgentSummary[]> {
  return apiFetch<AgentSummary[]>("/me/agents");
}

export function useMyAgents(enabled = true) {
  return useQuery({
    queryKey: ["my-agents"],
    queryFn: listMyAgents,
    enabled,
    staleTime: 10_000,
  });
}
