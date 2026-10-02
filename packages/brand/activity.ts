import type { Fill } from "./api";

const HOUR = 3600;

export type Burst<T extends Fill = Fill> = T & { readonly n: number };

const joins = (burst: Burst, fill: Fill) =>
  burst.type === fill.type && burst.side === fill.side && burst.outcome === fill.outcome && burst.title === fill.title && burst.at - fill.at < HOUR;

export const bursts = <T extends Fill>(fills: readonly T[]): readonly Burst<T>[] =>
  fills.reduce<readonly Burst<T>[]>((out, fill) => {
    const last = out.at(-1);
    return last && joins(last, fill)
      ? out.with(-1, { ...last, n: last.n + 1, shares: last.shares + fill.shares, dollars: last.dollars + fill.dollars })
      : [...out, { ...fill, n: 1 }];
  }, []);

export const threads = (list: readonly Burst[]): readonly (readonly Burst[])[] =>
  list.reduce<readonly (readonly Burst[])[]>((out, burst) => {
    const thread = out.at(-1);
    return thread?.[0]?.title === burst.title ? out.with(-1, [...thread, burst]) : [...out, [burst]];
  }, []);
