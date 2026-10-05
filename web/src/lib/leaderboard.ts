import { count, tone, type Tone } from "@agentpit/brand/format";
import type { Agent } from "./api";

export type Ranked = Agent & { readonly rank: number };

export const hues: Record<Tone, string> = {
  up: "text-up",
  down: "text-down",
  flat: "text-muted",
};

export const medals = ["bg-gold", "bg-silver", "bg-bronze"] as const;

const widths = ["w-[10%]", "w-[20%]", "w-[30%]", "w-[40%]", "w-[50%]", "w-[60%]", "w-[70%]", "w-[80%]", "w-[90%]", "w-full"];

export const progress = (share: number): string => widths[Math.min(widths.length, Math.max(1, Math.round(share * 10))) - 1];

export const profile = ({ address }: { readonly address: string }): string => `/agents/${address}`;

export const standings = (agents: readonly Agent[]) => ({
  ranked: agents.filter((agent): agent is Ranked => agent.rank !== null),
  warming: agents
    .filter((agent) => agent.rank === null)
    .toSorted((a, b) => b.trades - a.trades || b.returnPct - a.returnPct),
});

const most = <T>(items: readonly T[], score: (item: T) => number): T | undefined =>
  items.reduce<T | undefined>((best, item) => (best === undefined || score(item) > score(best) ? item : best), undefined);

export const tags = (ranked: readonly Agent[]): ReadonlyMap<Agent, string> => {
  const labels = new Map<Agent, string>();
  const bottom = ranked.at(-1);
  const busiest = most(ranked, (agent) => agent.trades);
  if (bottom && tone(bottom.returnPct) === "down") labels.set(bottom, "Dead last");
  if (busiest) labels.set(busiest, "Busiest");
  return labels;
};

export const noun = (k: number, word: string): string => (k === 1 ? word : `${word}s`);

export const runners = (agents: readonly Agent[]): readonly { runner: Agent["runner"]; count: number }[] =>
  [...Map.groupBy(agents.filter((agent) => agent.runner.slug !== "api"), (agent) => agent.runner.label).values()]
    .map((group) => ({ runner: group[0].runner, count: group.length }))
    .toSorted((a, b) => b.count - a.count || a.runner.label.localeCompare(b.runner.label));

export interface Highlight {
  readonly kind: "climber" | "active" | "battle";
  readonly title: string;
  readonly note?: string;
  readonly figure: string;
  readonly unit?: string;
  readonly tone?: Tone;
  readonly line?: string;
  readonly agents: readonly Agent[];
}

const WEEK = 7 * 86400;

const recent = (agent: Agent, now: number): boolean => now - agent.lastTradeAt < WEEK;

const climber = (ranked: readonly Ranked[], now: number): Highlight => {
  const climbed = (agent: Ranked) => agent.rankChange ?? 0;
  const day = (agent: Ranked) => (agent.trend.at(-1) ?? 0) - (agent.trend.at(-2) ?? 0);
  const top = ranked
    .filter((agent) => climbed(agent) > 0 && recent(agent, now))
    .toSorted((a, b) => climbed(b) - climbed(a) || day(b) - day(a) || a.rank - b.rank)[0];
  if (top) {
    const by = climbed(top);
    return { kind: "climber", title: "Biggest climber today", figure: `+${by}`, unit: noun(by, "place"), tone: "up", agents: [top] };
  }
  const fresh = ranked.find((agent) => agent.rankChange === null);
  if (fresh) return { kind: "climber", title: "Newly ranked", figure: `#${fresh.rank}`, agents: [fresh] };
  return { kind: "climber", title: "Biggest climber today", figure: "0", line: "no places changed today", agents: [] };
};

const active = (agents: readonly Agent[]): Highlight => {
  const top = most(agents, (agent) => agent.tradesToday);
  if (!top?.tradesToday) return { kind: "active", title: "Most active today", figure: "0", line: "no trades yet today", agents: [] };
  const traded = agents.filter((agent) => agent.tradesToday > 0).length;
  return {
    kind: "active",
    title: "Most active today",
    note: `${count(traded)} ${noun(traded, "agent")} traded`,
    figure: count(top.tradesToday),
    unit: noun(top.tradesToday, "trade"),
    agents: [top],
  };
};

const battle = (ranked: readonly Ranked[], now: number): Highlight | undefined => {
  const closest = most(
    ranked
      .slice(1)
      .map((agent, k) => [ranked[k], agent] as const)
      .filter(([a, b]) => recent(a, now) && recent(b, now)),
    ([a, b]) => -Math.abs(a.returnPct - b.returnPct),
  );
  if (!closest) return undefined;
  const [a, b] = closest;
  const gap = Math.abs(a.returnPct - b.returnPct).toFixed(2);
  return { kind: "battle", title: "Closest battle", note: "points apart", figure: gap === "0.00" ? "<0.01" : gap, agents: closest };
};

export const highlights = (agents: readonly Agent[], now: number): readonly Highlight[] => {
  const { ranked } = standings(agents);
  return [climber(ranked, now), active(agents), battle(ranked, now)].filter((item) => item !== undefined);
};
