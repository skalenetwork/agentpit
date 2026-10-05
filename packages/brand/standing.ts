import { type Call, type Profile, RANK_FLOOR } from "./api";
import { ACTIVE_SECONDS } from "./format";

export type RankLabel =
  | { readonly kind: "medal"; readonly place: 1 | 2 | 3; readonly text: string }
  | { readonly kind: "ranked"; readonly text: string }
  | { readonly kind: "warming"; readonly text: string };

export const rankLabel = (place: number | null, rankedCount: number, trades: number): RankLabel => {
  if (place === null) return { kind: "warming", text: `${trades} of ${RANK_FLOOR}` };
  const text = `#${place} of ${rankedCount}`;
  const medal = ([1, 2, 3] as const).find((step) => step === place);
  return medal ? { kind: "medal", place: medal, text } : { kind: "ranked", text };
};

export const recordCalls = ({ wins, losses, best, worst }: Pick<NonNullable<Profile["record"]>, "wins" | "losses" | "best" | "worst">): readonly { readonly label: string; readonly call: Call }[] => {
  if (wins + losses === 1) return [{ label: "Only call", call: best }];
  const [high, low] = best.pnl < 0 ? (["Smallest loss", "Biggest loss"] as const) : worst.pnl > 0 ? (["Biggest win", "Smallest win"] as const) : (["Best call", "Worst call"] as const);
  return [
    { label: high, call: best },
    { label: low, call: worst },
  ];
};

export const isActive = (lastTradeAt: number | null, now: number) => lastTradeAt !== null && now - lastTradeAt < ACTIVE_SECONDS;
