import { trendChart } from "@agentpit/brand/chart";
import { tone } from "@agentpit/brand/format";
import { useId } from "react";

const STROKE = { fill: "none", strokeWidth: 1.5, strokeLinejoin: "round", strokeLinecap: "round", vectorEffect: "non-scaling-stroke" } as const;
const DOT = { up: "text-up", down: "text-down", flat: "text-muted" } as const;

export const Chart = ({ trend, hot, onHot }: { trend: readonly number[]; hot: number | null; onHot: (index: number | null) => void }) => {
  const id = useId();
  const { d, zero, points } = trendChart(trend);
  const last = points.length - 1;
  const at = hot ?? last;
  const dot = points[at];
  const area = `${d}L1000,${zero}L0,${zero}Z`;
  return (
    <div
      role="img"
      aria-label={`Earned at each daily close, ${trend.length} closes`}
      className="relative h-28 touch-none select-none motion-safe:animate-draw sm:h-32"
      onPointerMove={(event) => {
        const box = event.currentTarget.getBoundingClientRect();
        onHot(Math.min(last, Math.max(0, Math.round(((event.clientX - box.left) / box.width) * last))));
      }}
      onPointerLeave={() => onHot(null)}
    >
      <svg className="absolute inset-0 size-full overflow-visible" viewBox="0 0 1000 1000" preserveAspectRatio="none" aria-hidden="true">
        <clipPath id={`${id}up`}>
          <rect x="-50" y="-100" width="1100" height={zero + 100} />
        </clipPath>
        <clipPath id={`${id}down`}>
          <rect x="-50" y={zero} width="1100" height={1100 - zero} />
        </clipPath>
        <line x1="0" x2="1000" y1={zero} y2={zero} className="stroke-line" strokeDasharray="3 3" vectorEffect="non-scaling-stroke" />
        <path d={area} className="fill-up/6" clipPath={`url(#${id}up)`} />
        <path d={area} className="fill-down/6" clipPath={`url(#${id}down)`} />
        <path d={d} className="stroke-up" clipPath={`url(#${id}up)`} {...STROKE} />
        <path d={d} className="stroke-down" clipPath={`url(#${id}down)`} {...STROKE} />
      </svg>
      {dot && hot !== null && <div className="pointer-events-none absolute inset-y-0 w-px bg-line" style={{ left: `${dot.x / 10}%` }} />}
      {dot && (
        <div
          className={`pointer-events-none absolute size-1.75 -translate-1/2 rounded-control bg-current ring-3 ring-current/20 ${DOT[tone(trend[at] ?? 0)]}`}
          style={{ left: `${dot.x / 10}%`, top: `${dot.y / 10}%` }}
        />
      )}
    </div>
  );
};

export const Spark = ({ trend }: { trend: readonly number[] }) => {
  const line = trendChart(trend);
  return (
    <svg className={`h-6 w-24 overflow-visible ${DOT[line.tone]}`} viewBox="0 0 1000 1000" preserveAspectRatio="none" aria-hidden="true">
      <line x2="1000" y1={line.zero} y2={line.zero} className="stroke-line" vectorEffect="non-scaling-stroke" />
      <path d={line.d} className="stroke-current" {...STROKE} />
    </svg>
  );
};
