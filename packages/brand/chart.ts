import type { Race, Standing } from "./api";
import { shortDay, type Tone, tone } from "./format";

export interface Point {
  readonly x: number;
  readonly y: number;
}

export interface TrendChart {
  readonly d: string;
  readonly zero: number;
  readonly tone: Tone;
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
  return { d: points.map(({ x, y }, i) => `${i ? "L" : "M"}${x},${y}`).join(""), zero: level(0), tone: tone(trend.at(-1) ?? 0) };
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

export interface RaceLane {
  readonly standing: Standing;
  readonly hue: number;
  readonly own: boolean;
  readonly d: string;
  readonly start: Point | null;
  readonly end: Point & { readonly dx: number };
  readonly bell: number | null;
  readonly side: "left" | "right";
}

export interface RaceTick {
  readonly x: number;
  readonly dx: number;
  readonly label: string;
  readonly anchor: "start" | "middle" | "end";
  readonly now: boolean;
}

export const FINAL_GAP = 32;
const [WIDE, PHONE, ROOM, DAY] = [460, 260, 44, 86_400];

const elapsed = ({ startsAt, endsAt }: Race, at: number) => Math.min(1, Math.max(0, (at - startsAt) / (endsAt - startsAt)));

export const raceChart = (race: Race, agent?: string): { readonly levels: readonly number[]; readonly lanes: readonly RaceLane[] } | null => {
  const rows = Math.min(10, race.ranked);
  const { at, lines } = race.series;
  if (rows < 2 || at.length < 2) return null;
  const xs = [...at, race.phase === "live" ? race.builtAt : race.endsAt].map((t) => elapsed(race, t));
  const level = (rank: number) => round((100 * (rank - 1)) / (rows - 1), 2);
  const placed = (rank: number | null | undefined): rank is number => rank != null && rank <= rows;
  const spot = ({ x, rank }: { readonly x: number; readonly rank: number }) => ({ x: round(100 * x, 2), y: level(rank) });
  const mine = (address: string) => address.toLowerCase() === agent?.toLowerCase();
  const standings = new Map(race.standings.map((standing) => [standing.address, standing]));
  return {
    levels: Array.from({ length: rows }, (_, k) => level(k + 1)),
    lanes: lines
      .flatMap(({ address, ranks }) => {
        const standing = standings.get(address);
        const points = (race.final ? xs.slice(0, -1) : xs).map((x, i) => (placed(ranks[i]) ? { x, rank: ranks[i] } : null));
        const [bell, current] = [ranks.at(-2), ranks.at(-1)];
        const last = points.findLast((point) => point !== null);
        const end = race.final && placed(current) ? { x: 100, y: level(current), dx: FINAL_GAP } : last && { ...spot(last), dx: 0 };
        return standing && end && (placed(current) || mine(address))
          ? [{ standing, points, end, bell: race.final && placed(current) && placed(bell) ? level(bell) : null, outside: !placed(current), first: ranks.findIndex((rank) => rank !== null) }]
          : [];
      })
      .toSorted((a, b) => Number(a.outside) - Number(b.outside) || a.first - b.first || a.standing.address.localeCompare(b.standing.address))
      .map(({ standing, points, end, bell }, k): RaceLane => {
        const first = points.find((point) => point !== null);
        return {
          standing,
          hue: k % 10,
          own: mine(standing.address),
          d: points
            .map((point, i) => {
              const [prev, next] = [points[i - 1], points[i + 1]];
              return point && !(prev?.rank === point.rank && next?.rank === point.rank) ? `${prev ? "L" : "M"}${round(1000 * point.x)},${point.rank - 1}` : "";
            })
            .join(""),
          start: first ? spot(first) : null,
          end,
          bell,
          side: (end.x / 100) * WIDE + end.dx + 20 + 7 * standing.name.length <= WIDE ? "right" : "left",
        };
      }),
  };
};

export const raceTicks = (race: Race, phone = false): readonly RaceTick[] => {
  const width = phone ? PHONE : WIDE;
  const days = (race.endsAt - race.startsAt) / DAY;
  const every = (days > 20 ? 7 : days >= 8 ? 3 : 1) * (phone ? 2 : 1);
  const now = race.phase === "live" ? elapsed(race, race.builtAt) * width : null;
  const marks = [...(now === null ? [] : [now]), race.final ? width + FINAL_GAP : width];
  const date = (at: number) => shortDay(new Date(at * 1000));
  const tick = (x: number, label: string, anchor: RaceTick["anchor"], dx = 0): RaceTick => ({ x: round((100 * x) / width, 2), dx, label, anchor, now: false });
  return [
    ...Array.from({ length: Math.floor(days / every + 0.5) }, (_, k) => k * every)
      .filter((day) => marks.every((mark) => Math.abs(mark - (day / days) * width) >= ROOM))
      .map((day) => tick((day / days) * width, date(race.startsAt + day * DAY), day ? "middle" : "start")),
    ...(now === null ? [] : [{ ...tick(now, "Now", "middle"), now: true }]),
    ...(race.final ? [tick(width, "Final", "middle", FINAL_GAP)] : now === null || width - now >= ROOM ? [tick(width, date(race.endsAt), "end")] : []),
  ];
};
