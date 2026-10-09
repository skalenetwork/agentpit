import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { claimPositionRequest } from "@/api/portfolio";
import { ApiError } from "@/api/client";
import { claimErrorMessage } from "@/lib/claimError";
import { Button } from "@/components/ui/button";

/** Collects a won-but-unclaimed position. The row itself carries the
 *  `conditionId` — never a market id — which is why `/positions/claim`
 *  resolves it rather than taking one directly. */
export function ClaimButton({
  conditionId,
  userAddress,
}: {
  conditionId: string;
  userAddress: string;
}) {
  const queryClient = useQueryClient();
  const refreshPositions = () => {
    void queryClient.invalidateQueries({
      queryKey: ["positions", userAddress],
    });
    // The claimed position becomes a closed one -- Biggest Win, P/L and
    // Predictions all read off `closedPositions`, and without this they'd
    // sit stale (still counting the position as open/unclaimed) until
    // whatever next natural refetch happens to invalidate it.
    void queryClient.invalidateQueries({
      queryKey: ["closed-positions", userAddress],
    });
  };
  const claim = useMutation({
    // The server takes one transaction lock per ACCOUNT, so clicks on several
    // rows at once would toast 409s for claims about to be paid. Mutations of
    // one scope run one after the other: later rows wait ("Claiming…").
    scope: { id: `claim:${userAddress}` },
    mutationFn: () => claimPositionRequest(conditionId),
    onSuccess: () => {
      toast.success("Claimed.");
      refreshPositions();
      // Claiming pays out apUSD, so the balance at the top of the page
      // changes. The top-up and the claim also move the native buffer, so
      // `credits` goes too: its tile is hidden, but stays honest if shown.
      void queryClient.invalidateQueries({
        queryKey: ["balance-allowance", "COLLATERAL"],
      });
      void queryClient.invalidateQueries({ queryKey: ["credits"] });
    },
    onError: (err) => {
      const message =
        err instanceof ApiError
          ? claimErrorMessage(err.status, err.body)
          : "Failed to claim.";
      toast.error(message);
      // A 400 usually means a background auto-redeem pass already paid this
      // row, which the list shows as unclaimed until its 10 s staleTime runs
      // out: refresh it now so the dead Claim button goes. Other failures say
      // nothing about whether the position is still there.
      if (err instanceof ApiError && err.status === 400) refreshPositions();
    },
  });

  return (
    <Button
      size="sm"
      variant="outline"
      className="shrink-0"
      disabled={claim.isPending}
      onClick={() => claim.mutate()}
    >
      {claim.isPending ? "Claiming…" : "Claim"}
    </Button>
  );
}
