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
  const claim = useMutation({
    mutationFn: () => claimPositionRequest(conditionId),
    onSuccess: () => {
      toast.success("Claimed.");
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
      // Claiming pays out apUSD, so the balance at the top of the page
      // changes. Without this it sits stale until whatever next natural
      // refetch happens to invalidate it. The platform pays the claim's gas
      // now, but the top-up and the claim still move the wallet's native
      // buffer, so `credits` is invalidated too: the Credits tile is hidden
      // today, and this keeps it honest the day it is shown again.
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
