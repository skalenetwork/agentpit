import { ago, type Tone } from "@agentpit/brand/format";
import { isActive } from "@agentpit/brand/standing";
import type { ReactNode } from "react";

export const TONE: Readonly<Record<Tone, string>> = { up: "text-up", down: "text-down", flat: "text-ink" };

export const Sep = () => <span className="text-faint">·</span>;

export const LastTrade = ({ at, now, prefix }: { at: number; now: number; prefix?: ReactNode }) =>
  isActive(at, now) ? (
    <span className="text-ink">Active now</span>
  ) : (
    <span className="inline-flex items-center gap-1.5">
      {prefix}
      {ago(at, now)}
    </span>
  );
