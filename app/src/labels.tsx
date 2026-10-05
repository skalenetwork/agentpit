import { ago, type Tone } from "@agentpit/brand/format";
import { isActive } from "@agentpit/brand/standing";
import type { ReactNode } from "react";

export const TONE: Readonly<Record<Tone, string>> = { up: "text-up", down: "text-down", flat: "text-ink" };

export const Sep = () => <span className="text-faint">·</span>;

export const LastTrade = ({ at, now, prefix }: { at: number; now: number; prefix?: ReactNode }) =>
  isActive(at, now) ? (
    <span className="inline-flex items-center gap-1.5 text-ink">
      <span className="relative flex size-2">
        <span className="absolute inset-0 rounded-control bg-up opacity-75 motion-safe:animate-ping" />
        <span className="relative size-2 rounded-control bg-up" />
      </span>
      Active now
    </span>
  ) : (
    <span className="inline-flex items-center gap-1.5">
      {prefix}
      {ago(at, now)}
    </span>
  );
