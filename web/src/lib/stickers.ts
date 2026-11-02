import type { Prize } from "@agentpit/brand/api";
import { count, dollars } from "@agentpit/brand/format";

export type Metal = "gold" | "silver" | "bronze";
export type Enamel = "cobalt" | "jade" | "rose" | "violet" | "tangerine" | "teal";
export type Shape = "burst" | "scallop" | "facet" | "hex" | "crest" | "roundel" | "squircle";
export type Cut = "banner" | "tag" | "stub" | "chamfer" | "capsule" | "pennant";

export const materials = ["foil", "soft", "flat", "line"] as const;
export const palettes = ["medal", "pastel", "noir"] as const;
export type Material = (typeof materials)[number];
export type Palette = (typeof palettes)[number];
export const look: { readonly material: Material; readonly palette: Palette; readonly pool: Material } = { material: "flat", palette: "pastel", pool: "foil" };

export const metals: Record<Metal, readonly string[]> = {
  gold: ["#6b4500", "#b98410", "#ffeeb0", "#e3b440", "#8a5e05", "#f8dc7e", "#a37210"],
  silver: ["#5f6a7d", "#c3ccda", "#ffffff", "#a5b0c2", "#6c778a", "#eef2f8", "#8c97aa"],
  bronze: ["#4a2209", "#a85a26", "#f3bb8c", "#c27440", "#6a3312", "#e09a64", "#8a481d"],
};

export const enamels: Record<Enamel, readonly [string, string]> = {
  cobalt: ["#6f9bff", "#1c3cb0"],
  jade: ["#3cc9a8", "#065e51"],
  rose: ["#ff86b0", "#a81c48"],
  violet: ["#ae95ff", "#4728b0"],
  tangerine: ["#ffa04d", "#a83b05"],
  teal: ["#4fcbe2", "#065d70"],
};

const point = (t: number, r: number) => `${(50 + r * Math.cos(t)).toFixed(2)} ${(50 + r * Math.sin(t)).toFixed(2)}`;
const polar = (steps: number, r: (k: number, t: number) => number, turn = -Math.PI / 2) =>
  `M${Array.from({ length: steps }, (_, k) => {
    const t = turn + (k / steps) * 2 * Math.PI;
    return point(t, r(k, t));
  }).join("L")}Z`;

export const octagon = (r: number) => polar(8, () => r, -Math.PI / 2 + Math.PI / 8);

export const shapes: Record<Shape, string> = {
  burst: polar(56, (k) => (k % 2 ? 40.5 : 46)),
  scallop: polar(240, (_, t) => 41 + 5 * Math.abs(Math.cos(8 * (t + Math.PI / 2)))),
  facet: octagon(46),
  hex: polar(6, () => 46),
  crest: "M13 9Q50 1 87 9V47Q87 78 50 95Q13 78 13 47Z",
  roundel: polar(96, () => 45),
  squircle: "M50 5C86 5 95 14 95 50S86 95 50 95 5 86 5 50 14 5 50 5Z",
};

export const ribbon = "M30 64 18 117 29 109 37 120 47 70ZM70 64 82 117 71 109 63 120 53 70Z";

export const facets = (outer: number, inner: number) => {
  const at = (k: number) => -Math.PI / 2 + Math.PI / 8 + (k * Math.PI) / 4;
  return Array.from({ length: 8 }, (_, k) => ({
    d: `M${point(at(k), outer)}L${point(at(k + 1), outer)}L${point(at(k + 1), inner)}L${point(at(k), inner)}Z`,
    light: Math.cos(at(k) + Math.PI / 8 + (3 * Math.PI) / 4),
  }));
};

const podium: readonly { readonly shape: Shape; readonly finish: Metal }[] = [
  { shape: "burst", finish: "gold" },
  { shape: "scallop", finish: "silver" },
  { shape: "facet", finish: "bronze" },
];
const pins: readonly Shape[] = ["hex", "crest", "roundel", "squircle"];
const cuts: readonly Cut[] = ["banner", "tag", "stub", "chamfer", "capsule", "pennant"];
const colors: readonly Enamel[] = ["cobalt", "jade", "rose", "violet", "tangerine", "teal"];

const enamel = (place: number) => {
  const k = place - 4;
  return colors[(k + Math.floor(k / colors.length)) % colors.length];
};
const rim = (place: number): Metal => (place % 2 ? "silver" : "gold");

export const seal = (place: number) => podium[place - 1] ?? { shape: pins[(place - 4) % pins.length], finish: enamel(place), rim: rim(place) };
export const cut = (place: number) => ({ cut: cuts[(place - 4) % cuts.length], enamel: enamel(place), rim: rim(place) });

const ordinal = new Intl.PluralRules("en-US", { type: "ordinal" });
const suffixes: Partial<Record<Intl.LDMLPluralRule, string>> = { one: "st", two: "nd", few: "rd" };
export const nth = (n: number) => `${n}${suffixes[ordinal.select(n)] ?? "th"}`;

const compact = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });
export const short = (n: number) => (n >= 10_000 && n % (n >= 1e6 ? 1e5 : 100) === 0 ? compact.format(n) : count(n));

export const face = ({ usd, skl }: Prize) => (usd > 0 ? { figure: usd >= 10_000 ? `$${short(usd)}` : dollars(usd), unit: "" } : { figure: short(skl), unit: "SKL" });
export const tiny = ({ usd, skl }: Prize) => [usd > 0 ? dollars(usd) : "", skl > 0 ? `${short(skl)} SKL` : ""].filter(Boolean).join(" + ");
export const fit = (text: string) => (text.length <= 3 ? "text-[27cqw]" : text.length === 4 ? "text-[24cqw]" : text.length === 5 ? "text-[20cqw]" : text.length === 6 ? "text-[17.5cqw]" : "text-[15cqw]");
