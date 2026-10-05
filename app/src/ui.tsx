import { rankMove } from "@agentpit/brand/format";
import logoUrl from "@agentpit/brand/logo.svg";
import { Check, Copy as CopyIcon, type LucideIcon, Trophy, X } from "lucide-react";
import { type ComponentProps, lazy, type ReactNode, Suspense, useEffect, useRef, useState } from "react";

const Glyph = lazy(() => import("./glyph"));

const VARIANT = {
  primary: "bg-ink text-paper hover:opacity-85",
  secondary: "bg-surface text-ink hover:bg-line",
  ghost: "text-muted hover:bg-surface hover:text-ink",
  danger: "bg-down text-paper hover:opacity-85",
  warn: "bg-down/10 text-down hover:bg-down/15 dark:bg-down/16 dark:hover:bg-down/22",
  paper: "bg-paper text-ink hover:bg-line",
};
const SIZE = { sm: "h-7.5 px-3.5", md: "h-8.5 px-4", lg: "h-10 px-5", icon: "size-7.5" };
export const SOFT = "rounded-card bg-surface/70";
export const MEDALS = ["bg-gold", "bg-silver", "bg-bronze"] as const;

type Look = { variant?: keyof typeof VARIANT; size?: keyof typeof SIZE };
type ButtonProps = ComponentProps<"button"> & Look;
type CardProps = { title: string; icon: LucideIcon; note?: ReactNode; children?: ReactNode } & (
  | { figure?: never; unit?: never; sub?: never }
  | { figure: ReactNode; unit?: ReactNode; sub?: ReactNode }
);

export const Button = ({ variant = "secondary", size = "sm", className = "", ...props }: ButtonProps | (ComponentProps<"a"> & Look & { href: string })) => {
  const look = `inline-flex shrink-0 items-center justify-center gap-1.5 rounded-control text-small font-medium whitespace-nowrap transition-[background-color,color,opacity,scale] duration-150 ease-out motion-safe:active:scale-[0.97] disabled:pointer-events-none disabled:opacity-50 ${SIZE[size]} ${VARIANT[variant]} ${className}`;
  return "href" in props ? <a {...props} className={look} /> : <button type="button" {...props} className={look} />;
};

export const Input = ({ className = "", ...props }: ComponentProps<"input">) => (
  <input
    {...props}
    className={`h-10 w-full rounded-control border border-ink/15 bg-paper px-4 text-small text-ink placeholder:text-faint focus:border-signal focus:ring-2 focus:ring-signal/25 focus:outline-none ${className}`}
  />
);

export const Logo = ({ height = 16 }: { height?: number }) => (
  <span role="img" aria-label="AgentPit" className="block bg-ink" style={{ height, width: (height * 857) / 156, mask: `url("${logoUrl}") center / contain no-repeat` }} />
);

export const Robot = ({ address, className }: { address: string; className: string }) => (
  <img src={`https://agentpit.dev/agents/${address.toLowerCase()}/avatar.svg`} alt="" className={`shrink-0 rounded-robot bg-surface ${className}`} />
);

export const RankMove = ({ change }: { change: number | null }) => {
  const move = rankMove(change);
  return (
    move && (
      <span
        role="img"
        aria-label={`${move.dir === "up" ? "Up" : "Down"} ${move.by} since yesterday`}
        className={`text-caption font-medium tabular-nums ${move.dir === "up" ? "text-up" : "text-down"}`}
      >
        {move.dir === "up" ? "▲" : "▼"}
        {move.by}
      </span>
    )
  );
};

export const RankPill = ({ place, change, rankedCount }: { place: number | null; change: number | null; rankedCount: number }) => {
  const medal = place === null ? undefined : MEDALS[place - 1];
  return (
    <span className="inline-flex items-center gap-2">
      <span className={`inline-flex h-6 shrink-0 items-center gap-1 rounded-control px-2.5 text-caption font-medium tabular-nums text-ink ${medal ? `scheme-light ${medal}` : "bg-surface"}`}>
        {medal && <Trophy size={12} strokeWidth={1.75} />}
        {place === null ? "Warming up" : `#${place} of ${rankedCount}`}
      </span>
      <RankMove change={change} />
    </span>
  );
};

export const Card = ({ title, icon: Icon, note, figure, unit, sub, children }: CardProps) => (
  <section className={`min-w-0 p-5 sm:p-6 ${SOFT}`}>
    <div className="flex h-5 items-center gap-1.5 text-label font-medium text-muted">
      <Icon size={14} strokeWidth={1.75} className="shrink-0" aria-hidden="true" />
      <h2 className="truncate">{title}</h2>
      {note && <span className="flex-1 truncate pl-3 text-right font-normal tabular-nums">{note}</span>}
    </div>
    {figure !== undefined && (
      <>
        <p className="mt-2 flex min-w-0 items-baseline gap-1.5">
          <span className="text-stat whitespace-nowrap text-ink tabular-nums">{figure}</span>
          {unit && <span className="truncate text-body/5 text-muted">{unit}</span>}
        </p>
        {sub && <p className="mt-1.5 truncate text-label text-muted tabular-nums">{sub}</p>}
      </>
    )}
    {children && <div className="mt-4">{children}</div>}
  </section>
);

export const Human = (props: { seed: string; size: number }) => (
  <Suspense fallback={<span className="shrink-0 rounded-control bg-surface" style={{ width: props.size, height: props.size }} />}>
    <Glyph {...props} />
  </Suspense>
);

export const Copy = ({ text, ...props }: ButtonProps & { text: string }) => {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), 1500);
    return () => clearTimeout(timer);
  }, [copied]);
  return (
    <Button {...props} onClick={() => navigator.clipboard.writeText(text).then(() => setCopied(true))}>
      {copied ? <Check size={14} strokeWidth={1.75} /> : <CopyIcon size={14} strokeWidth={1.75} />}
      <span className={props.size === "icon" ? "sr-only" : undefined}>{copied ? "Copied" : "Copy"}</span>
    </Button>
  );
};

export const Well = ({ text, mono = false }: { text: string; mono?: boolean }) => (
  <div className="inline-flex max-w-full items-center gap-3 rounded-well bg-surface p-1.5 pl-4 text-left">
    <p className={`min-w-0 flex-1 ${mono ? "font-mono text-caption break-all" : "font-medium"}`}>{text}</p>
    <Copy text={text} variant="primary" />
  </div>
);

export const Sheet = ({ title, onClose, locked = false, children }: { title: string; onClose: () => void; locked?: boolean; children: ReactNode }) => {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    if (!ref.current?.open) ref.current?.showModal();
    ref.current?.querySelector("input")?.focus();
  }, []);
  return (
    <dialog
      ref={ref}
      onClose={onClose}
      onCancel={(event) => locked && event.preventDefault()}
      closedby={locked ? "none" : "any"}
      className="m-auto w-[calc(100%-32px)] max-w-sheet rounded-panel bg-paper p-6 shadow-overlay"
    >
      <div className="flex items-start justify-between gap-4">
        <h2 className="text-body font-medium">{title}</h2>
        {!locked && (
          <Button variant="ghost" size="icon" aria-label="Close" onClick={onClose} className="-my-0.5 -mr-1.5">
            <X size={14} strokeWidth={1.75} />
          </Button>
        )}
      </div>
      {children}
    </dialog>
  );
};

export const Fault = ({ children }: { children: ReactNode }) => <p className="mt-2 text-caption text-down">{children}</p>;

export const Note = ({ children }: { children: ReactNode }) => <p className="py-10 text-muted">{children}</p>;
