import { shortDay } from "@agentpit/brand/format";
import { useId } from "react";

const PAD = 0.06;
const STROKE = { fill: "none", strokeWidth: 1.5, strokeLinejoin: "round", strokeLinecap: "round", vectorEffect: "non-scaling-stroke" } as const;

export const closeDay = (closes: number, index: number, now: number) => (index === closes - 1 ? "Today" : shortDay(new Date((now - (closes - 1 - index) * 86400) * 1000)));

export const Chart = ({ trend, hot, onHot }: { trend: readonly number[]; hot: number | null; onHot: (index: number | null) => void }) => {
  const id = useId();
  const last = trend.length - 1;
  const top = Math.max(0, ...trend);
  const bottom = Math.min(0, ...trend);
  const y = (value: number) => PAD + ((1 - 2 * PAD) * (top - value)) / (top - bottom || 1);
  const path = trend.map((value, i) => `${i ? "L" : "M"}${((i / last) * 1000).toFixed(1)},${(y(value) * 1000).toFixed(1)}`).join("");
  const zero = y(0) * 1000;
  const shown = hot ?? last;
  const end = trend[shown] ?? 0;
  return (
    <div
      role="img"
      aria-label={`Earned at each daily close, ${trend.length} closes`}
      className="relative mt-6 h-40 touch-none select-none"
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
        <line x1="0" x2="1000" y1={zero} y2={zero} className="stroke-line" vectorEffect="non-scaling-stroke" />
        <path d={path} className="stroke-up" clipPath={`url(#${id}up)`} {...STROKE} />
        <path d={path} className="stroke-down" clipPath={`url(#${id}down)`} {...STROKE} />
      </svg>
      {hot !== null && <div className="pointer-events-none absolute inset-y-0 w-px bg-line" style={{ left: `${(hot / last) * 100}%` }} />}
      <div
        className={`pointer-events-none absolute size-2 -translate-1/2 rounded-control ring-2 ring-paper ${hot !== null ? "bg-ink" : end > 0 ? "bg-up" : end < 0 ? "bg-down" : "bg-muted"}`}
        style={{ left: `${(shown / last) * 100}%`, top: `${y(end) * 100}%` }}
      />
    </div>
  );
};
