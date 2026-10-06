import { shortDay, type Tone, tone } from "./format";

export interface Point {
  readonly x: number;
  readonly y: number;
}

export interface TrendChart {
  readonly d: string;
  readonly zero: number;
  readonly points: readonly Point[];
  readonly tone: Tone;
}

export interface Bar extends Point {
  readonly height: number;
  readonly tone: "up" | "down";
}

const PAD = 0.06;
const [WIDTH, HEIGHT, INSET] = [96, 28, 3];

const round = (value: number, places = 1) => +value.toFixed(places);

export const trendChart = (trend: readonly number[]): TrendChart => {
  const last = trend.length - 1;
  const top = Math.max(0, ...trend);
  const bottom = Math.min(0, ...trend);
  const level = (value: number) => round(1000 * (PAD + ((1 - 2 * PAD) * (top - value)) / (top - bottom || 1)));
  const points = trend.map((value, i) => ({ x: round(1000 - (1000 * (last - i)) / (last || 1)), y: level(value) }));
  return { d: points.map(({ x, y }, i) => `${i ? "L" : "M"}${x},${y}`).join(""), zero: level(0), points, tone: tone(trend.at(-1) ?? 0) };
};

export const sparkline = (values: readonly number[], days: number): { readonly points: string; readonly end: Point } | null => {
  const top = Math.max(...values);
  const span = top - Math.min(...values) || 1;
  const points = values.map((value, i) => ({
    x: round(INSET + ((days - values.length + i) / (days - 1)) * (WIDTH - 2 * INSET)),
    y: round(INSET + ((top - value) / span) * (HEIGHT - 2 * INSET)),
  }));
  const end = points.at(-1);
  return points.length > 1 && end ? { points: points.map(({ x, y }) => `${x},${y}`).join(" "), end } : null;
};

export const recordBars = (pnls: readonly number[]): { readonly zero: number; readonly width: number; readonly bars: readonly Bar[] } => {
  const up = Math.max(0, ...pnls);
  const scale = 100 / (up + Math.max(0, ...pnls.map((pnl) => -pnl)) || 1);
  const zero = up * scale;
  const slot = 100 / Math.max(pnls.length, 16);
  return {
    zero: round(zero, 2),
    width: round(slot * 0.7, 2),
    bars: pnls.map((pnl, i) => {
      const height = Math.max(6, Math.abs(pnl) * scale);
      return { x: round(i * slot + slot * 0.15, 2), y: round(pnl > 0 ? zero - height : zero, 2), height: round(height, 2), tone: pnl > 0 ? "up" : "down" };
    }),
  };
};

export const closeLabel = (trendStart: string | null, index: number, length: number) =>
  index === length - 1 ? "Today" : trendStart ? shortDay(new Date(Date.parse(trendStart) + index * 86_400_000)) : "";

export const earnedToday = (earned: number, trend: readonly number[]) => earned - (trend.at(-2) ?? 0);
