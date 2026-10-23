import { type EarnedChart, moneyTone } from "@agentpit/brand/bets";
import { trendChart } from "@agentpit/brand/chart";
import { useId } from "react";
import { HUE } from "./labels";

const STROKE = { fill: "none", strokeWidth: 1.5, strokeLinejoin: "round", strokeLinecap: "round", vectorEffect: "non-scaling-stroke" } as const;

export const EarnedLine = ({ chart, hot, onHot }: { chart: EarnedChart; hot: number | null; onHot: (index: number | null) => void }) => {
  const id = useId();
  const { points, line, area, zero, fill, label } = chart;
  const dot = points[hot ?? points.length - 1];
  return (
    <div
      role="img"
      aria-label={label}
      className="relative mt-4 h-36 touch-none select-none sm:h-44"
      onPointerMove={(event) => {
        const box = event.currentTarget.getBoundingClientRect();
        const gaps = points.map((point) => Math.abs(point.x - ((event.clientX - box.left) / box.width) * 1000));
        onHot(gaps.indexOf(Math.min(...gaps)));
      }}
      onPointerLeave={() => onHot(null)}
    >
      <svg className="absolute inset-0 size-full overflow-visible" aria-hidden="true">
        <line x1="0" x2="100%" y1={`${zero / 10}%`} y2={`${zero / 10}%`} className="stroke-line" strokeWidth="1" strokeDasharray="3 3" />
        <svg viewBox="0 0 1000 1000" preserveAspectRatio="none" width="100%" height="100%" overflow="visible" aria-hidden="true">
          <clipPath id={`${id}up`}>
            <rect x="-50" y="-200" width="1100" height={zero + 200} />
          </clipPath>
          <clipPath id={`${id}down`}>
            <rect x="-50" y={zero} width="1100" height={1200 - zero} />
          </clipPath>
          {fill && <path d={area} className="fill-up/6 dark:fill-up/10" clipPath={`url(#${id}up)`} />}
          {fill && <path d={area} className="fill-down/6 dark:fill-down/10" clipPath={`url(#${id}down)`} />}
          <path d={line} className="stroke-up" clipPath={`url(#${id}up)`} {...STROKE} />
          <path d={line} className="stroke-down" clipPath={`url(#${id}down)`} {...STROKE} />
        </svg>
      </svg>
      {dot && hot !== null && <div className="pointer-events-none absolute inset-y-0 w-px bg-line" style={{ left: `${dot.x / 10}%` }} />}
      {dot && (
        <span
          className={`pointer-events-none absolute size-1.75 -translate-1/2 rounded-control bg-current ring-3 ring-current/20 ${HUE[moneyTone(dot.value)]}`}
          style={{ left: `${dot.x / 10}%`, top: `${dot.y / 10}%` }}
        />
      )}
    </div>
  );
};

export const Spark = ({ trend }: { trend: readonly number[] }) => {
  const line = trendChart(trend);
  return (
    <svg className={`h-6 w-24 overflow-visible ${HUE[line.tone]}`} viewBox="0 0 1000 1000" preserveAspectRatio="none" aria-hidden="true">
      <line x2="1000" y1={line.zero} y2={line.zero} className="stroke-line" vectorEffect="non-scaling-stroke" />
      <path d={line.d} className="stroke-current" {...STROKE} />
    </svg>
  );
};
