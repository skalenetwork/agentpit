import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";

export interface AgentRunner {
  slug: string;
  label: string;
  host: string | null;
}

export interface AgentSummary {
  handle: string | null;
  eth_address: string;
  created_at: number;
  runner: AgentRunner;
}

export interface CreatedAgent extends AgentSummary {
  api_key: string;
}

const MY_AGENTS = ["my-agents"];

export function listMyAgents(): Promise<AgentSummary[]> {
  return apiFetch<AgentSummary[]>("/me/agents");
}

export function useMyAgents(enabled = true) {
  return useQuery({
    queryKey: MY_AGENTS,
    queryFn: listMyAgents,
    enabled,
    staleTime: 10_000,
  });
}

export function useCreateAgent() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => apiFetch<CreatedAgent>("/me/agents", { method: "POST" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: MY_AGENTS }),
    gcTime: 0,
  });
}

export function useRenameAgent(address: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (handle: string) =>
      apiFetch<AgentSummary>(`/me/agents/${address}`, {
        method: "PATCH",
        body: JSON.stringify({ handle }),
      }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: MY_AGENTS }),
  });
}

export function useDeleteAgent(address: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () =>
      apiFetch<void>(`/me/agents/${address}`, { method: "DELETE" }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: MY_AGENTS }),
  });
}
