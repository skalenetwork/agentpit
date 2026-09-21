import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/api/client";

export interface Position {
  proxyWallet: string;
  asset: string;          // token_id
  conditionId: string;
  size: number;           // display shares
  avgPrice: number;
  curPrice: number;
  cashPnl: number;
  percentPnl: number;
  initialValue: number;   // cost basis (avg x size)
  currentValue: number;
  outcome: string;
  outcomeIndex: number;
  oppositeOutcome: string;
  oppositeAsset: string;
  title: string;
  slug: string;
  /** Slug of the event grouping this market; "" when it belongs to none. */
  eventSlug: string;
  icon: string;
  redeemable: boolean;
  /** What the live bids actually return for the whole position, and how many
   *  shares they can absorb — short of `size` on a thin book, 0 with no bids.
   *  `currentValue` is `curPrice x size` off the book midpoint, which is above
   *  what a sell executes at, so these are the numbers to show beside a Sell. */
  sellableValue: number;
  sellableSize: number;
  /** The market resolved. A loser is never redeemed and so never leaves
   *  /positions; without this it sits under Active at zero forever. */
  settled: boolean;
  endDate: string;        // market end/resolution time, unix seconds (string)
}

export async function getPositions(userAddress: string): Promise<Position[]> {
  return apiFetch<Position[]>(`/positions?user=${encodeURIComponent(userAddress)}`);
}

export function usePositions(userAddress: string | undefined) {
  return useQuery({
    queryKey: ["positions", userAddress],
    queryFn: () => {
      if (!userAddress) throw new Error("userAddress is required");
      return getPositions(userAddress);
    },
    enabled: Boolean(userAddress),
    staleTime: 10_000,
  });
}

export async function getClosedPositions(
  userAddress: string,
): Promise<Position[]> {
  return apiFetch<Position[]>(
    `/closed-positions?user=${encodeURIComponent(userAddress)}`,
  );
}

export function useClosedPositions(userAddress: string | undefined) {
  return useQuery({
    queryKey: ["closed-positions", userAddress],
    queryFn: () => {
      if (!userAddress) throw new Error("userAddress is required");
      return getClosedPositions(userAddress);
    },
    enabled: Boolean(userAddress),
    staleTime: 10_000,
  });
}

export async function getUsdcBalance(): Promise<number> {
  const r = await apiFetch<{ balance: string }>("/balance-allowance?asset_type=COLLATERAL");
  return Number(r.balance) / 1_000_000;
}

export function useUsdcBalance(enabled = true) {
  return useQuery({
    queryKey: ["balance-allowance", "COLLATERAL"],
    queryFn: getUsdcBalance,
    enabled,
    staleTime: 10_000,
  });
}

export interface CreditsBalance {
  credits_wei: string;
}

/** The wallet's native balance — what pays for a transaction, as a wei
 *  string. It stays a string end to end so the caller's own formatter
 *  (`formatCredits`) does the arithmetic; wei overflows `Number`'s safe
 *  integer range for any account that has been topped up a few times. */
export async function getCredits(): Promise<string> {
  const r = await apiFetch<CreditsBalance>("/me/credits");
  return r.credits_wei;
}

export function useCredits(enabled = true) {
  return useQuery({
    queryKey: ["credits"],
    queryFn: getCredits,
    enabled,
    staleTime: 10_000,
  });
}

export interface TopUpStatus {
  nextAllowedAt: number;
}

/** GET is a cheap database-only read (no chain call), so it's safe to fetch on
 *  every profile page load — that's how the button knows to render disabled
 *  for a user who topped up an hour ago, before they've clicked anything. */
export async function getTopUpStatus(): Promise<TopUpStatus> {
  return apiFetch<TopUpStatus>("/me/top-up");
}

export function useTopUpStatus(enabled = true) {
  return useQuery({
    queryKey: ["top-up-status"],
    queryFn: getTopUpStatus,
    enabled,
    staleTime: 10_000,
  });
}

export interface TopUpResult {
  balance: string;
  minted: string;
  nextAllowedAt: number;
}

/** What the button says. A part-hour rounds UP: "Available in 0h" reads as a
 *  bug, and rounding down would invite a click that fails. */
export function topUpLabel(nextAllowedAt: number, now: number): string {
  if (now >= nextAllowedAt) return "Top up to $100k";
  return `Available in ${Math.ceil((nextAllowedAt - now) / 3600)}h`;
}

/** The button's disabled state and label are derived from the fetched status
 *  query — never from the mutation's result. `topUp.data` is undefined until
 *  the user actually clicks, so a version that read `nextAllowedAt` off the
 *  mutation would always render enabled on page load, even mid-cooldown. This
 *  function can't make that mistake: it has no access to mutation data at all. */
export function topUpButtonState(
  status: TopUpStatus | undefined,
  isPending: boolean,
  now: number,
): { disabled: boolean; label: string } {
  const nextAllowedAt = status?.nextAllowedAt ?? 0;
  return {
    disabled: isPending || now < nextAllowedAt,
    label: isPending ? "Topping up…" : topUpLabel(nextAllowedAt, now),
  };
}

export function claimPositionRequest(conditionId: string): Promise<unknown> {
  return apiFetch<unknown>("/positions/claim", {
    method: "POST",
    body: JSON.stringify({ condition_id: conditionId }),
  });
}

export function useTopUp() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => apiFetch<TopUpResult>("/me/top-up", { method: "POST" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: ["balance-allowance", "COLLATERAL"],
      });
      void queryClient.invalidateQueries({ queryKey: ["top-up-status"] });
    },
  });
}
