import { Bar, BarChart, CartesianGrid, Line, LineChart, ReferenceLine, Tooltip, XAxis, YAxis } from "recharts";
import { count, signedDollars } from "../../lib/format";

export interface DailyChartProps {
  readonly kind: "agents" | "trades" | "pnl";
  readonly points: readonly { readonly label: string; readonly value: number | null }[];
}

const axis = { tickLine: false, axisLine: false, tickMargin: 8, minTickGap: 8 } as const;

export default function DailyChart({ kind, points }: DailyChartProps) {
  const Chart = kind === "trades" ? BarChart : LineChart;
  const format = kind === "pnl" ? signedDollars : count;
  return (
    <Chart
      data={points}
      responsive
      className="size-full text-caption [&_.recharts-bar-rectangle_path]:fill-signal [&_.recharts-cartesian-axis-tick-value]:fill-muted [&_.recharts-cartesian-grid_line]:stroke-line [&_.recharts-line-curve]:stroke-signal [&_.recharts-reference-line-line]:stroke-muted [&_.recharts-tooltip-cursor]:stroke-line [&_g]:outline-none"
    >
      <CartesianGrid vertical={false} strokeDasharray="3 3" />
      <XAxis dataKey="label" {...axis} />
      <YAxis {...axis} width="auto" allowDecimals={false} tickFormatter={format} />
      <Tooltip
        isAnimationActive={false}
        cursor={kind !== "trades" && { strokeDasharray: "3 3", strokeWidth: 0.8 }}
        content={({ active, payload: [item], label }) =>
          active &&
          typeof item?.value === "number" && (
            <div className="rounded-panel border border-line bg-paper px-3 py-2">
              <p className="text-muted">{label}</p>
              <p className="font-medium text-ink tabular-nums">{format(item.value)}</p>
            </div>
          )
        }
      />
      {kind === "pnl" && <ReferenceLine y={0} />}
      {kind === "trades" ? (
        <Bar dataKey="value" radius={2} isAnimationActive={false} />
      ) : (
        <Line dataKey="value" type={kind === "agents" ? "stepAfter" : "linear"} strokeWidth={1.5} dot={false} activeDot={false} isAnimationActive={false} />
      )}
    </Chart>
  );
}
