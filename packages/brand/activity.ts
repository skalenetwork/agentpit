import type { Fill } from "./api";

const HOUR = 3600;

export type Burst<T extends Fill = Fill> = T & { readonly fills: readonly T[] };

const joins = (burst: Burst, fill: Fill) =>
  burst.tx === null && fill.tx === null && burst.type === fill.type && burst.side === fill.side && burst.outcome === fill.outcome && burst.title === fill.title && burst.at - fill.at < HOUR;

export const bursts = <T extends Fill>(fills: readonly T[]): readonly Burst<T>[] =>
  fills.reduce<readonly Burst<T>[]>((out, fill) => {
    const last = out.at(-1);
    return last && joins(last, fill)
      ? out.with(-1, { ...last, shares: last.shares + fill.shares, dollars: last.dollars + fill.dollars, fills: [...last.fills, fill] })
      : [...out, { ...fill, fills: [fill] }];
  }, []);
