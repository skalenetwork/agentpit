import { ago, shortDay } from "@agentpit/brand/format";
import type { Agent } from "./api";

export const signedPercent = (value: number) => `${value > 0 ? "+" : value < 0 ? "−" : ""}${Math.abs(value).toFixed(2)}%`;

export const tone = (value: number) => (Math.abs(value) < 0.005 ? "text-ink" : value > 0 ? "text-up" : "text-down");

export const when = (at: number, now: number) => (now - at < 86400 ? ago(at, now) : shortDay(new Date(at * 1000)));

export const LastTrade = ({ agent, now }: { agent: Agent; now: number }) =>
  agent.last_trade_at === null ? (
    "No trades yet"
  ) : now - agent.last_trade_at < 3600 ? (
    <span className="inline-flex items-center gap-1.5 text-ink">
      <span className="size-1.5 rounded-control bg-up" />
      Trading now
    </span>
  ) : (
    `Last trade ${ago(agent.last_trade_at, now)}`
  );
