import { INK_RUNNERS, type Runner as RunnerInfo } from "@agentpit/brand/api";
import logoUrl from "@agentpit/brand/logo.svg";
import { Check, Copy as CopyIcon, X } from "lucide-react";
import { type ComponentProps, lazy, type ReactNode, Suspense, useEffect, useRef, useState } from "react";

const Glyph = lazy(() => import("./glyph"));
const MARKS = import.meta.glob<string>("../../packages/brand/runners/*.svg", { query: "?url", import: "default", eager: true });

const VARIANT = {
  primary: "bg-ink text-paper hover:opacity-85",
  secondary: "bg-surface text-ink hover:bg-line",
  ghost: "text-muted hover:bg-surface hover:text-ink",
  danger: "bg-down text-paper hover:opacity-85",
  warn: "text-down hover:bg-down/8",
};
const SIZE = { sm: "h-8 px-3.5", md: "h-9 px-4", lg: "h-10 px-5" };

type ButtonProps = ComponentProps<"button"> & { variant?: keyof typeof VARIANT; size?: keyof typeof SIZE };

export const Button = ({ variant = "secondary", size = "sm", className = "", ...props }: ButtonProps) => (
  <button
    type="button"
    {...props}
    className={`inline-flex shrink-0 items-center justify-center gap-1.5 rounded-control text-small font-medium whitespace-nowrap transition-[background-color,color,opacity,scale] duration-150 ease-out motion-safe:active:scale-[0.97] disabled:pointer-events-none disabled:opacity-50 ${SIZE[size]} ${VARIANT[variant]} ${className}`}
  />
);

export const Input = ({ className = "", ...props }: ComponentProps<"input">) => (
  <input
    {...props}
    className={`h-10 w-full rounded-control border border-ink/15 bg-paper px-4 text-small text-ink placeholder:text-faint focus:border-signal focus:ring-2 focus:ring-signal/25 focus:outline-none ${className}`}
  />
);

export const Logo = ({ height = 18 }: { height?: number }) => (
  <span role="img" aria-label="AgentPit" className="block bg-ink" style={{ height, width: (height * 857) / 156, mask: `url("${logoUrl}") center / contain no-repeat` }} />
);

export const Robot = ({ address, size }: { address: string; size: number }) => (
  <img src={`https://agentpit.dev/agents/${address}/avatar.svg`} width={size} height={size} alt="" className="shrink-0 rounded-robot bg-surface" />
);

export const Runner = ({ runner }: { runner: RunnerInfo }) => {
  const url = MARKS[`../../packages/brand/runners/${runner.slug}.svg`];
  const mark = "mr-1.5 inline-block size-3.5 align-[-2px]";
  return (
    <>
      {INK_RUNNERS.has(runner.slug) ? <span className={`${mark} bg-current`} style={{ mask: `url("${url}") center / contain no-repeat` }} /> : <img src={url} alt="" className={mark} />}
      {runner.host ? `${runner.label}, ${runner.host.toLowerCase()}` : runner.label}
    </>
  );
};

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
      {copied ? "Copied" : "Copy"}
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
          <button
            type="button"
            aria-label="Close"
            onClick={onClose}
            className="-mt-1 -mr-2 grid size-8 place-items-center rounded-control text-muted transition-colors duration-150 hover:bg-surface hover:text-ink"
          >
            <X size={16} strokeWidth={1.75} />
          </button>
        )}
      </div>
      {children}
    </dialog>
  );
};

export const Fault = ({ children }: { children: ReactNode }) => <p className="mt-2 text-caption text-down">{children}</p>;

export const Note = ({ children }: { children: ReactNode }) => <p className="py-10 text-muted">{children}</p>;

export const Fact = ({ label, children }: { label: string; children: ReactNode }) => (
  <div className="flex min-h-12 items-center gap-4 py-2.5">
    <dt className="w-32 shrink-0 text-muted sm:w-40">{label}</dt>
    <dd className="flex min-w-0 flex-1 items-center justify-between gap-4">{children}</dd>
  </div>
);
