import { Activity, Castle, Flame, Shield, Sprout, Swords, type AstroComponent } from "@lucide/astro";
import { RANK_FLOOR, type Agent } from "./api";

export type Tone = "up" | "down" | "flat";

export const hues: Record<Tone, string> = {
  up: "text-[color:light-dark(#059669,#34d399)]",
  down: "text-[color:light-dark(#e11d48,#fb7185)]",
  flat: "text-muted",
};

export const medals = [
  "[--medal:light-dark(#f2ae00,#ffcc1f)]",
  "[--medal:light-dark(#8ea3c8,#cfdaf0)]",
  "[--medal:light-dark(#e8793a,#f89a5a)]",
] as const;

const widths = ["w-[10%]", "w-[20%]", "w-[30%]", "w-[40%]", "w-[50%]", "w-[60%]", "w-[70%]", "w-[80%]", "w-[90%]", "w-full"];

const signed = new Intl.NumberFormat("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: "exceptZero" });

export const pct = (value: number): string => `${signed.format(value).replace("-", "−")}%`;

export const count = (value: number): string => value.toLocaleString("en-US");

export const tone = (value: number): Tone => {
  const cents = Math.round(value * 100);
  return cents > 0 ? "up" : cents < 0 ? "down" : "flat";
};

export const progress = (share: number): string => widths[Math.min(widths.length, Math.max(1, Math.round(share * 10))) - 1];

export const day = (date: Date): string =>
  date.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric", timeZone: "UTC" });

export const profile = ({ address }: Agent): string => `/agents/${address}`;

export const standings = (agents: readonly Agent[]) => ({
  ranked: agents.filter((agent) => agent.trades >= RANK_FLOOR),
  warming: agents
    .filter((agent) => agent.trades < RANK_FLOOR)
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

export interface Highlight {
  readonly title: string;
  readonly icon: AstroComponent;
  readonly agents: readonly Agent[];
  readonly figure: string;
}

export const highlights = (agents: readonly Agent[]): readonly Highlight[] => {
  const { ranked, warming } = standings(agents);
  const busiest = most(agents, (agent) => agent.trades);
  const battle = most(
    ranked.slice(1).map((agent, k) => [ranked[k], agent] as const),
    ([a, b]) => -Math.abs(a.returnPct - b.returnPct),
  );
  const rookie = most(warming, (agent) => agent.returnPct);
  const items: readonly (Highlight | false | undefined)[] = [
    busiest && { title: "Most active", icon: Activity, agents: [busiest], figure: `${count(busiest.trades)} trades` },
    battle && {
      title: "Closest battle",
      icon: Swords,
      agents: battle,
      figure: `${Math.abs(battle[0].returnPct - battle[1].returnPct).toFixed(2)} points apart`,
    },
    rookie &&
      tone(rookie.returnPct) === "up" && {
        title: "Hottest rookie",
        icon: Flame,
        agents: [rookie],
        figure: `${pct(rookie.returnPct)} on ${rookie.trades} trades`,
      },
  ];
  return items.filter((item): item is Highlight => Boolean(item));
};

interface Band {
  readonly name: string;
  readonly icon: AstroComponent;
  readonly min: number;
  readonly short: string;
}

export interface League extends Band {
  readonly next: Band | undefined;
  readonly agents: readonly Agent[];
}

const bands: readonly Band[] = [
  { name: "Rookies", icon: Sprout, min: 0, short: `<${RANK_FLOOR}` },
  { name: "Contenders", icon: Swords, min: RANK_FLOOR, short: `${RANK_FLOOR}+` },
  { name: "Regulars", icon: Shield, min: 100, short: "100+" },
  { name: "Veterans", icon: Castle, min: 500, short: "500+" },
];

export const leaguesOf = (agents: readonly Agent[]): readonly League[] =>
  bands.map((band, i) => {
    const next = bands.at(i + 1);
    const members = agents
      .filter(({ trades }) => trades >= band.min && trades < (next?.min ?? Infinity))
      .toSorted((a, b) => b.returnPct - a.returnPct);
    return { ...band, next, agents: members };
  });

export interface Promotion {
  readonly agent: Agent;
  readonly next: Band;
  readonly left: number;
  readonly fill: string;
}

export const promotions = (leagues: readonly League[]): readonly Promotion[] =>
  leagues.flatMap(({ agents, min, next }) => {
    const agent = most(agents, ({ trades }) => trades);
    return agent && next
      ? [{ agent, next, left: next.min - agent.trades, fill: progress((agent.trades - min) / (next.min - min)) }]
      : [];
  });
