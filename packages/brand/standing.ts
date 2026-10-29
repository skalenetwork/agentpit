import { RANK_FLOOR } from "./api";
import { ACTIVE_SECONDS } from "./format";

export type RankLabel =
  | { readonly kind: "medal"; readonly place: 1 | 2 | 3; readonly text: string }
  | { readonly kind: "ranked"; readonly text: string }
  | { readonly kind: "warming"; readonly text: string };

export const rankLabel = (place: number | null, rankedCount: number, markets: number): RankLabel => {
  if (place === null) return { kind: "warming", text: `${markets} of ${RANK_FLOOR}` };
  const text = `#${place} of ${rankedCount}`;
  const medal = ([1, 2, 3] as const).find((step) => step === place);
  return medal ? { kind: "medal", place: medal, text } : { kind: "ranked", text };
};

export const isActive = (lastTradeAt: number | null, now: number) => lastTradeAt !== null && now - lastTradeAt < ACTIVE_SECONDS;
