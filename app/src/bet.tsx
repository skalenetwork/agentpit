import { type Bet, explorerTx } from "@agentpit/brand/api";
import { betCaption, FILLS_SHOWN, facts, fillCounts, fillLine, moneyTone, type PriceTag, percent, priceChart, quiet, STATUS, thinLine, utc, visible } from "@agentpit/brand/bets";
import { chance, count, dollars, exactDollars, signedDollars } from "@agentpit/brand/format";
import polymarket from "@agentpit/brand/polymarket.svg";
import { X } from "lucide-react";
import { Fragment, useId, useState } from "react";
import { HUE, Sep } from "./labels";
import { Thumb } from "./thumb";
import { Button, Modal } from "./ui";

const TINT: Readonly<Record<string, string>> = { Yes: "bg-yes/10 text-yes dark:bg-yes/16", No: "bg-no/10 text-no dark:bg-no/16" };
const CHIP: Partial<Record<Bet["status"], string>> = { won: "bg-up/10 text-up dark:bg-up/16", lost: "bg-down/10 text-down dark:bg-down/16" };
const BUY = "inline-block size-1.75 rounded-control bg-up";
const SELL = "inline-block size-2 rounded-control border-[1.5px] border-ink bg-paper";

export const Outcome = ({ outcome }: { outcome: string }) => (
  <span className={`inline-flex h-6 shrink-0 items-center rounded-control px-2.5 text-label font-medium ${TINT[outcome] ?? "bg-paper text-ink"}`}>{outcome}</span>
);

const PriceChart = ({ bet, now }: { bet: Bet; now: number }) => {
  const chart = priceChart(bet, now);
  const tags: Readonly<Record<PriceTag["kind"], string>> = { exit: chart.quiet ? "font-medium fill-muted" : "font-medium fill-ink", entry: "fill-muted", tick: "fill-faint" };
  return (
    <div className="pr-22">
      <div className="relative h-40" role="img" aria-label={chart.label}>
        <svg className="absolute inset-0 size-full overflow-visible" aria-hidden="true">
          {[chart.hundred, chart.zero].map((y) => (
            <line key={y} x1="0" x2="100%" y1={`${y}%`} y2={`${y}%`} className="stroke-line" strokeWidth="1" />
          ))}
          {[chart.entry, chart.soldAvg].map(
            (mark, i) => mark && <line key={i ? "sold" : "entry"} x1={`${mark.x}%`} x2="100%" y1={`${mark.y}%`} y2={`${mark.y}%`} className="stroke-faint" strokeWidth="1" strokeDasharray="3 3" />,
          )}
          <svg viewBox="0 0 1000 1000" preserveAspectRatio="none" width="100%" height="100%" overflow="visible" aria-hidden="true">
            <path d={chart.line} className={chart.quiet ? "stroke-muted" : "stroke-ink"} fill="none" strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke" />
          </svg>
          {chart.dots.map(({ x, y, side }) =>
            side === "BUY" ? (
              <circle key={`${side}${x}`} cx={`${x}%`} cy={`${y}%`} r="3.5" className="fill-up" />
            ) : (
              <circle key={`${side}${x}`} cx={`${x}%`} cy={`${y}%`} r="3.25" className="fill-paper stroke-ink" strokeWidth="1.5" />
            ),
          )}
          {chart.end !== null && <circle cx="100%" cy={`${chart.end}%`} r="4" className={chart.quiet ? "fill-muted stroke-muted/15" : "fill-ink stroke-ink/15"} strokeWidth="6" paintOrder="stroke" />}
          {chart.tags.map(({ y, text, kind }) => (
            <text key={`${kind}${text}`} x="100%" dx="12" y={`${y}%`} dominantBaseline="central" className={`text-caption tabular-nums ${tags[kind]}`}>
              {text}
            </text>
          ))}
        </svg>
      </div>
      <p className="mt-1 grid grid-cols-[1fr_auto_1fr] gap-4 text-caption whitespace-nowrap text-muted tabular-nums">
        <span>{chart.left}</span>
        {chart.legend && (
          <span className="flex items-center gap-3">
            <span className="flex items-center gap-1.5">
              <span className={BUY} />
              Buy
            </span>
            <span className="flex items-center gap-1.5">
              <span className={SELL} />
              Sell
            </span>
          </span>
        )}
        <span className="col-start-3 text-right">{chart.right}</span>
      </p>
    </div>
  );
};

export const BetSheet = ({ bet, now, onClose }: { bet: Bet; now: number; onClose: () => void }) => {
  const [all, setAll] = useState(false);
  const caption = betCaption(bet, now);
  const thin = thinLine(bet);
  const cut = all ? bet.fills.length : visible(bet.fills.length, FILLS_SHOWN);
  const title = useId();
  return (
    <Modal
      onClose={onClose}
      labelledBy={title}
      className="m-auto max-h-[calc(100dvh-32px)] w-[560px] max-w-[calc(100vw-32px)] flex-col overflow-hidden rounded-panel bg-paper p-0 text-small text-ink shadow-overlay open:flex max-sm:mb-0 max-sm:max-h-[calc(100dvh-48px)] max-sm:w-full max-sm:max-w-none max-sm:rounded-b-none"
    >
      <span className="mx-auto my-2 block h-1 w-9 shrink-0 rounded-control bg-line sm:hidden" aria-hidden="true" />
      <button
        type="button"
        aria-label="Close"
        onClick={(event) => event.currentTarget.closest("dialog")?.close()}
        className="absolute top-2 right-2 z-1 grid size-8 place-items-center rounded-control bg-paper text-muted transition-colors duration-150 hover:bg-surface hover:text-ink"
      >
        <X className="size-4" strokeWidth={1.75} aria-hidden="true" />
      </button>
      <div className="relative min-h-0 overflow-y-auto overscroll-contain p-5 max-sm:pt-0 max-sm:pb-8">
        <header className="flex items-start gap-4 pr-8">
          <Thumb src={bet.icon} title={bet.title} category={bet.category} className="size-12 rounded-[4px]!" />
          <div className="min-w-0 flex-1">
            <h2 id={title} className="line-clamp-2 text-title text-pretty text-ink max-sm:line-clamp-none">
              {bet.title}
            </h2>
            {(caption.event || caption.text) && (
              <p className="mt-1 flex min-w-0 items-center gap-1.5 overflow-hidden text-caption whitespace-nowrap text-muted">
                {caption.event && <span className="truncate">{caption.event}</span>}
                {caption.event && caption.text && <Sep />}
                {caption.text && <span className={`${caption.event ? "shrink-0" : "truncate"} ${caption.live ? "text-signal" : ""}`}>{caption.text}</span>}
              </p>
            )}
            <p className="mt-3 flex flex-wrap items-center gap-x-1.5 gap-y-2 text-label text-muted tabular-nums">
              <span className={`inline-flex h-6 shrink-0 items-center rounded-control px-2.5 text-label font-medium whitespace-nowrap ${CHIP[bet.status] ?? "bg-ink/7 text-ink"}`}>
                {STATUS[bet.status]}
              </span>
              <Sep />
              <span>{dollars(bet.cost)} on</span>
              <span className="flex max-w-full min-w-0 items-center gap-1.5">
                <span className={`inline-flex min-w-0 ${TINT[bet.outcome] ? "" : "*:bg-ink/7 dark:*:bg-ink/10"}`}>
                  <Outcome outcome={bet.outcome} />
                </span>
                <span className="whitespace-nowrap">at {chance(bet.entry)}</span>
              </span>
            </p>
          </div>
        </header>
        <div className="mt-6 tabular-nums">
          <p className="flex items-baseline gap-3">
            <span className={`text-stat ${quiet(bet) ? "text-muted" : HUE[moneyTone(bet.pnl)]}`}>{signedDollars(bet.pnl)}</span>
            <span className="text-small font-medium text-muted" title={`On the ${dollars(bet.cost)} put in`}>
              {percent(bet.pnl / bet.cost)}
            </span>
          </p>
          {thin && <p className="mt-1 text-caption text-muted">{thin}</p>}
        </div>
        <div className="mt-6">
          <PriceChart bet={bet} now={now} />
        </div>
        <dl className="mt-6 grid text-small sm:grid-cols-2 sm:gap-x-8">
          {facts(bet, now).map(([term, value]) => (
            <div key={term} className="flex min-h-10 items-center justify-between gap-4 border-t border-line py-2">
              <dt className="shrink-0 text-muted">{term}</dt>
              <dd className="min-w-0 text-right text-ink tabular-nums">{value}</dd>
            </div>
          ))}
        </dl>
        <section className="mt-8">
          <h3 className="flex h-5 items-center justify-between gap-3 text-label text-muted">
            Activity
            <span className="flex items-center gap-1.5 tabular-nums">
              {fillCounts(bet).map((part, i) => (
                <Fragment key={part}>
                  {i > 0 && <Sep />}
                  <span>{part}</span>
                </Fragment>
              ))}
            </span>
          </h3>
          <ul className="mt-2">
            {bet.fills.slice(0, cut).map((fill) => (
              <li key={`${fill.at}${fill.side}${fill.shares}${fill.dollars}${fill.tx}`} className="flex items-start justify-between gap-4 border-t border-line py-2.5">
                <div className="min-w-0">
                  <p className="text-ink">{fillLine(fill)}</p>
                  <p className="mt-0.5 text-caption text-muted tabular-nums">
                    {fill.tx ? (
                      <a
                        className="decoration-faint underline-offset-[3px] transition-colors duration-150 hover:text-ink hover:underline"
                        href={explorerTx(fill.tx)}
                        target="_blank"
                        rel="noopener"
                        aria-label={`${utc(fill.at, now)}, settlement transaction on the explorer`}
                      >
                        {utc(fill.at, now)}
                      </a>
                    ) : (
                      utc(fill.at, now)
                    )}
                  </p>
                </div>
                <p className="text-ink tabular-nums">{exactDollars(fill.dollars)}</p>
              </li>
            ))}
          </ul>
          {cut < bet.fills.length && (
            <div className="mt-3 flex">
              <Button variant="secondary" onClick={() => setAll(true)}>
                Show {count(bet.fills.length - cut)} more
              </Button>
            </div>
          )}
        </section>
        {bet.url && (
          <div className="mt-6 flex flex-wrap items-center gap-2">
            <Button href={bet.url} variant="secondary" target="_blank" rel="noopener">
              <span aria-hidden="true" className="size-3.5 shrink-0 bg-current" style={{ mask: `url("${polymarket}") center / contain no-repeat` }} />
              Open on Polymarket
            </Button>
          </div>
        )}
      </div>
    </Modal>
  );
};
