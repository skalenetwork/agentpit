import { type Bet, type BetStatus, explorerAddress, type Profile, type RaceEntry, type RaceHead, stageText } from "@agentpit/brand/api";
import { betTabs, earnedChart, highlights, moneyTone, quiet, rowCaption, SHOWN, stakeLine, today, visible } from "@agentpit/brand/bets";
import { chance, clock, count, dollars, pct, pool, shortAddress, shortDay, signedDollars, span, tone, utcTime } from "@agentpit/brand/format";
import { getRouteApi, useNavigate } from "@tanstack/react-router";
import { ArrowUpRight, ChartLine, LogOut, type LucideIcon, Pencil, Share, Trash2, TrendingDown, TrendingUp, Trophy } from "lucide-react";
import { type FormEvent, Fragment, type ReactNode, useState } from "react";
import { type Agent, type Order, useAgents, useCompetitions, useDeleteAgent, useEntry, useOrders, useProfile, useRaceEntries, useRenameAgent } from "../api";
import { BetSheet, Outcome } from "../bet";
import { EarnedLine } from "../chart";
import { HUE, LastTrade, Sep, TONE } from "../labels";
import { ApiError } from "../session";
import { Crumbs } from "../shell";
import { Thumb } from "../thumb";
import { Actions, Button, Copy, Fault, Input, Note, RankPill, Robot, Sheet, SOFT } from "../ui";

type Part = readonly [text: string, tint?: string];
type TabKey = BetStatus | "orders";

const SITE = "https://agentpit.dev";
const ICON = "max-md:w-7.5 max-md:px-0";
const UNDERLINE = "underline decoration-faint underline-offset-[3px] transition-colors duration-150 hover:text-ink hover:decoration-current";
const CARD = `relative min-w-0 p-5 sm:p-6 ${SOFT}`;
const CHIP = "inline-flex max-w-full align-top max-sm:max-w-36 max-sm:*:block max-sm:*:max-w-full max-sm:*:truncate max-sm:*:leading-6";
const ROW =
  "relative -mx-5 grid-cols-[32px_minmax(0,1fr)_auto] items-baseline gap-x-4 px-5 py-3 before:absolute before:inset-x-5 before:top-0 before:h-px before:bg-line first:before:hidden last:pb-5 sm:-mx-6 sm:px-6 sm:before:inset-x-6 sm:last:pb-6";
const THUMB = "col-start-1 row-span-2 row-start-1 size-8 self-start sm:self-center";
const TITLE = "col-start-2 row-start-1 min-w-0 text-small text-ink max-sm:line-clamp-2 sm:truncate";
const LINE = "col-start-2 row-start-2 mt-0.5 min-w-0 text-label text-muted tabular-nums max-sm:col-end-4";
const LEAD = {
  live: ["Live", "text-signal"],
  upcoming: ["Entries open"],
  locked: ["Starting soon"],
  ended: ["Ended"],
} as const satisfies Record<RaceHead["phase"], Part>;

const route = getRouteApi("/console/agents/$address");

const Label = ({ icon: Icon, title, note }: { icon: LucideIcon; title: string; note?: ReactNode }) => (
  <div className="flex h-5 items-center gap-1.5 text-label text-muted">
    <Icon className="size-3.5 shrink-0" strokeWidth={1.75} aria-hidden="true" />
    <h2 className="truncate">{title}</h2>
    {note && <span className="ml-auto pl-3 text-caption whitespace-nowrap text-muted tabular-nums">{note}</span>}
  </div>
);

const Header = ({ agent, profile, now }: { agent: Agent; profile: Profile | undefined; now: number }) => {
  const change = profile ? today(profile.money) : 0;
  return (
    <header className="mt-5 flex flex-col gap-4 sm:mt-7 sm:flex-row sm:flex-wrap sm:items-center sm:justify-between sm:gap-x-8">
      <div className="flex min-w-0 items-start gap-3 sm:items-center">
        <Robot address={agent.eth_address} className="size-13" />
        <div className="grid min-w-0 gap-1">
          <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
            <h1 className="max-w-full truncate text-title">{agent.name}</h1>
            {profile && <RankPill place={profile.standing.place} change={profile.standing.placeChange} rankedCount={profile.standing.rankedCount} />}
          </div>
          <p className="flex flex-wrap items-center gap-x-1.5 text-caption text-muted *:whitespace-nowrap">
            <span>Runs on {agent.runner.label}</span>
            {profile ? (
              <>
                <Sep />
                <span>
                  {dollars(profile.money.deposited)} paper since {shortDay(new Date(profile.firstTradeAt * 1000))}
                </span>
                <Sep />
                {profile.money.capital < 1 ? <span>No money left</span> : <LastTrade at={profile.lastTradeAt} now={now} prefix="Last trade " />}
              </>
            ) : (
              agent.trades === 0 && (
                <>
                  <Sep />
                  <span>No trades yet</span>
                </>
              )
            )}
          </p>
        </div>
      </div>
      {profile && (
        <dl className="grid grid-cols-3 gap-3 sm:flex sm:shrink-0 sm:gap-10">
          {[
            { term: "Return", value: pct(profile.money.returnPct), hue: TONE[tone(profile.money.returnPct)] },
            { term: "Earned", value: signedDollars(profile.money.earned), hue: TONE[tone(Math.round(profile.money.earned))] },
            { term: "Today", value: signedDollars(change), hue: HUE[moneyTone(change)], title: "Change in Earned since 00:00 UTC" },
          ].map(({ term, value, hue, title }) => (
            <div key={term} title={title} className="grid min-w-0 content-start sm:justify-items-end sm:text-right">
              <dt className="text-caption text-muted">{term}</dt>
              <dd className={`truncate text-figure tabular-nums max-sm:text-title ${hue}`}>{value}</dd>
            </div>
          ))}
        </dl>
      )}
    </header>
  );
};

const placing = ({ race, standing, above, below }: RaceEntry): readonly Part[] => {
  const { rank, returnPct, behind, wouldRank, bids } = standing;
  if (race.phase === "upcoming" || race.phase === "locked") return [];
  if (standing.disqualified) return [["Out", "text-down"]];
  if (rank === null) return race.phase === "ended" ? [["Did not place"]] : wouldRank ? [["No trades yet"], [`one trade puts it #${wouldRank}`]] : [["No trades yet"]];
  const result: Part = [pct(returnPct), HUE[tone(returnPct)]];
  if (race.phase === "ended") return race.final ? [[`Finished #${rank} of ${race.ranked}`], result] : [[`#${rank} at the bell`], ["final coming in"]];
  const gap = rank === 1 ? below && `${dollars(below.behind ?? 0)} ahead of ${below.name}` : above && `${dollars(behind ?? 0)} behind ${above.name}`;
  const shift = bids?.rank && bids.rank !== rank ? `#${bids.rank} at the bids` : null;
  return [[`#${rank} of ${race.ranked}`], result, ...[gap, shift].flatMap((text): Part[] => (text ? [[text]] : []))];
};

const Entry = ({ entry, now }: { entry: RaceEntry; now: number }) => {
  const { race, standing } = entry;
  const leave = useEntry(standing.address);
  const parts: readonly Part[] = [race.phase === "ended" && race.final ? ["Final"] : LEAD[race.phase], ...(race.phase === "ended" ? [] : [[stageText(race, now)] as const]), ...placing(entry)];
  return (
    <li className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-4 border-t border-line py-3 first:border-t-0 first:pt-0 last:pb-0">
      <div className="min-w-0">
        <p className="truncate text-small text-ink">{race.title}</p>
        <p className="mt-0.5 text-label text-muted tabular-nums">
          {parts.map(([text, tint], i) => (
            <Fragment key={text}>
              {i > 0 && (
                <>
                  {" "}
                  <Sep />{" "}
                </>
              )}
              <span className={`whitespace-nowrap ${tint ?? ""}`}>{text}</span>
            </Fragment>
          ))}
        </p>
        {leave.isError && <Fault>{(leave.error instanceof ApiError && leave.error.detail) || "Could not leave. Try again in a moment."}</Fault>}
      </div>
      <div className="flex gap-2">
        {race.phase === "upcoming" && (
          <Button variant="warn" disabled={leave.isPending} onClick={() => leave.mutate({ slug: race.slug, enter: false })} className={ICON}>
            <LogOut size={14} strokeWidth={1.75} />
            <span className="max-md:sr-only">Leave</span>
          </Button>
        )}
        <Button href={`${SITE}/competitions/${race.slug}?agent=${standing.address}`} target="_blank" rel="noreferrer" variant="paper" className={ICON}>
          <ArrowUpRight size={14} strokeWidth={1.75} />
          <span className="max-md:sr-only">View</span>
        </Button>
      </div>
    </li>
  );
};

const EarnedCard = ({ profile }: { profile: Profile }) => {
  const [hot, setHot] = useState<number | null>(null);
  const chart = earnedChart(profile);
  const point = hot === null ? undefined : chart.points[hot];
  return (
    <article className={CARD}>
      <Label
        icon={ChartLine}
        title="Earned"
        note={
          point ? (
            <>
              {shortDay(new Date(point.at * 1000))} {clock(new Date(point.at * 1000))} <Sep /> <span className={HUE[moneyTone(point.value)]}>{signedDollars(point.value)}</span>
            </>
          ) : (
            `Since ${shortDay(new Date(profile.firstTradeAt * 1000))}`
          )
        }
      />
      <EarnedLine chart={chart} hot={hot} onHot={setHot} />
    </article>
  );
};

const Highlights = ({ bets, onOpen }: { bets: readonly Bet[]; onOpen: (id: string) => void }) => {
  const pair = highlights(bets);
  return (
    pair && (
      <div className="grid gap-5 sm:grid-cols-2">
        {(
          [
            [TrendingUp, "Best bet", pair.best],
            [TrendingDown, "Worst bet", pair.worst],
          ] as const
        ).map(([icon, title, bet]) => (
          <article key={title} className={`${CARD} transition-colors duration-150 hover:bg-surface active:bg-surface`}>
            <Label icon={icon} title={title} />
            <div className="relative mt-4 flex items-center gap-4">
              <Thumb src={bet.icon} title={bet.title} category={bet.category} className="size-16" />
              <div className="min-w-0">
                <p className={`truncate text-figure tabular-nums ${HUE[moneyTone(bet.pnl)]}`}>{signedDollars(bet.pnl)}</p>
                <p className="mt-1.5 flex h-6 min-w-0 items-center gap-1.5 text-label text-muted">
                  <span className={CHIP}>
                    <Outcome outcome={bet.outcome} />
                  </span>
                  <span className="truncate">{bet.title}</span>
                </p>
              </div>
            </div>
            <button type="button" onClick={() => onOpen(bet.id)} aria-label={`Details: ${bet.title}, ${bet.outcome}`} className="absolute inset-0 cursor-pointer rounded-card -outline-offset-2" />
          </article>
        ))}
      </div>
    )
  );
};

const BetRow = ({ bet, now, hidden, onOpen }: { bet: Bet; now: number; hidden: boolean; onOpen: (id: string) => void }) => {
  const { stake, price } = stakeLine(bet);
  const caption = rowCaption(bet, now);
  const when = caption.live ? <span className="text-signal">{caption.text}</span> : caption.text;
  return (
    <li className={`group/row ${ROW} transition-colors duration-150 hover:bg-paper/70 active:bg-paper/70 dark:hover:bg-surface dark:active:bg-surface ${hidden ? "hidden" : "grid"}`}>
      <Thumb src={bet.icon} title={bet.title} category={bet.category} className={THUMB} />
      <p className={TITLE}>{bet.title}</p>
      <div className="col-start-3 row-start-1 text-right whitespace-nowrap tabular-nums">
        <p className={`text-small font-medium ${quiet(bet) ? "text-muted" : HUE[moneyTone(bet.pnl)]}`}>{signedDollars(bet.pnl)}</p>
        <p className="mt-0.5 text-caption text-muted sm:hidden">{when}</p>
      </div>
      <p className={LINE}>
        <span className="whitespace-nowrap">{stake}</span>{" "}
        <span className={`-my-0.5 ${CHIP}`}>
          <Outcome outcome={bet.outcome} />
        </span>{" "}
        <span className="whitespace-nowrap">{price}</span>
      </p>
      <p className="col-start-3 row-start-2 mt-0.5 text-right text-caption whitespace-nowrap text-muted tabular-nums max-sm:hidden">{when}</p>
      <button
        type="button"
        onClick={() => onOpen(bet.id)}
        aria-label={`Details: ${bet.title}, ${bet.outcome}`}
        className="absolute inset-0 cursor-pointer -outline-offset-2 group-last/row:rounded-b-card"
      />
    </li>
  );
};

const OrderRow = ({ order, icon }: { order: Order; icon: string | null }) => (
  <li className={`grid ${ROW}`}>
    <Thumb src={icon} title={order.title} category={null} className={THUMB} />
    <p className={TITLE}>{order.title}</p>
    <p className="col-start-3 row-start-1 text-right text-small font-medium whitespace-nowrap text-ink tabular-nums">{dollars(Number(order.price) * Number(order.original_size))}</p>
    <p className={LINE}>
      <span className="whitespace-nowrap">
        {order.side === "BUY" ? "Buy" : "Sell"} {count(Number(order.original_size))}
      </span>{" "}
      <span className={`-my-0.5 ${CHIP}`}>
        <Outcome outcome={order.outcome} />
      </span>{" "}
      <span className="whitespace-nowrap">at {chance(Number(order.price))}</span>
    </p>
  </li>
);

const BetsCard = ({ profile, orders, now, onOpen }: { profile: Profile | undefined; orders: readonly Order[]; now: number; onOpen: (id: string) => void }) => {
  const [chosen, setChosen] = useState<TabKey>();
  const [all, setAll] = useState(false);
  const view = profile ? betTabs(profile) : null;
  const tabs = [...(view?.tabs ?? []), ...(orders.length ? [{ status: "orders" as const, label: "Orders", n: orders.length, shown: undefined }] : [])];
  const current = tabs.find((tab) => tab.status === chosen)?.status ?? view?.active ?? "orders";
  const bets = view?.tabs.find((tab) => tab.status === current)?.bets ?? [];
  const cut = all ? bets.length : visible(bets.length, SHOWN);
  if (!tabs.length) return null;
  return (
    <section className={`relative min-w-0 overflow-hidden px-5 pb-5 sm:px-6 sm:pb-6 ${SOFT}`} aria-label="Bets">
      <fieldset className="-mx-5 min-w-0 overflow-x-auto px-5 text-small shadow-[inset_0_-1px_0_var(--color-line)] [scrollbar-width:none] sm:-mx-6 sm:px-6">
        <legend className="sr-only">Bets by status</legend>
        <div className="flex gap-8 max-sm:grid max-sm:auto-cols-[minmax(max-content,1fr)] max-sm:grid-flow-col max-sm:justify-items-start max-sm:gap-3">
          {tabs.map(({ status, label, n, shown }) => (
            <button
              key={status}
              type="button"
              aria-pressed={status === current}
              onClick={() => {
                setChosen(status);
                setAll(false);
              }}
              className="group/tab relative flex cursor-pointer items-center gap-2 pt-6 pb-4 whitespace-nowrap outline-none after:absolute after:inset-x-0 after:bottom-0 after:h-0.5 after:rounded-full after:bg-ink after:opacity-0 aria-pressed:after:opacity-100 max-sm:flex-col max-sm:items-start max-sm:gap-0 max-sm:pt-5"
            >
              <span className="rounded-item group-focus-visible/tab:outline-2 group-focus-visible/tab:outline-offset-2 group-focus-visible/tab:outline-signal">
                <span className="font-medium text-muted transition-colors duration-150 group-hover/tab:text-ink group-aria-pressed/tab:text-ink">{label}</span>{" "}
                <span className="text-muted tabular-nums">{count(n)}</span>
              </span>
              {shown !== undefined && <span className={`tabular-nums ${HUE[moneyTone(shown)]}`}>{signedDollars(shown)}</span>}
            </button>
          ))}
        </div>
      </fieldset>
      {current === "orders" ? (
        <>
          <p className="pt-3 pb-1 text-caption text-muted">Resting orders fill when the Polymarket book reaches the price.</p>
          <ul className="-mb-5 sm:-mb-6">
            {orders.map((order) => (
              <OrderRow key={order.id} order={order} icon={profile?.bets.find((bet) => bet.title === order.title)?.icon ?? null} />
            ))}
          </ul>
        </>
      ) : (
        <>
          <ul className={cut < bets.length ? "-mb-3" : "-mb-5 sm:-mb-6"}>
            {bets.map((bet, i) => (
              <BetRow key={bet.id} bet={bet} now={now} hidden={i >= cut} onOpen={onOpen} />
            ))}
          </ul>
          {cut < bets.length && (
            <div className="mt-4 flex">
              <Button variant="paper" onClick={() => setAll(true)}>
                Show all {count(bets.length)}
              </Button>
            </div>
          )}
        </>
      )}
    </section>
  );
};

const Wallet = ({ address, valuedAt }: { address: string; valuedAt: number }) => (
  <footer className="mt-5 flex flex-wrap items-center gap-x-4 gap-y-1 text-caption text-muted">
    <span className="flex items-center gap-2">
      Wallet
      <span className="font-mono text-ink">{shortAddress(address)}</span>
      <Copy text={address} variant="ghost" size="icon" className="-ml-1" />
    </span>
    <a href={explorerAddress(address)} target="_blank" rel="noreferrer" className="flex items-center gap-1 whitespace-nowrap transition-colors duration-150 hover:text-ink">
      View on explorer
      <ArrowUpRight size={12} strokeWidth={1.75} />
    </a>
    <span className="sm:ml-auto">
      As of {utcTime(new Date(valuedAt * 1000))} <Sep /> Paper money on SKALE on Base
    </span>
  </footer>
);

const Rename = ({ agent, onClose }: { agent: Agent; onClose: () => void }) => {
  const rename = useRenameAgent(agent.eth_address);
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const name = String(new FormData(event.currentTarget).get("name"));
    if (name === agent.name) onClose();
    else rename.mutate(name, { onSuccess: onClose });
  };
  return (
    <Sheet title="Rename agent" onClose={onClose}>
      <form onSubmit={submit} className="mt-4">
        <Input name="name" defaultValue={agent.name} required maxLength={15} pattern="[a-zA-Z0-9_]+" title="Letters, digits and underscores" aria-label="Name" />
        {rename.isError && <Fault>{rename.error instanceof ApiError && rename.error.status === 409 ? "That name is taken." : "Could not rename the agent. Try again in a moment."}</Fault>}
        <Actions onClose={onClose}>
          <Button type="submit" variant="primary" size="lg" disabled={rename.isPending}>
            Save
          </Button>
        </Actions>
      </form>
    </Sheet>
  );
};

const Delete = ({ agent, onClose }: { agent: Agent; onClose: () => void }) => {
  const navigate = useNavigate();
  const remove = useDeleteAgent(agent.eth_address);
  return (
    <Sheet title={`Delete ${agent.name}?`} onClose={onClose}>
      <p className="mt-3 text-muted">
        Its wallet, positions and history are removed. This cannot be undone.{" "}
        {agent.runner.slug === "api" ? "Its API key stops working." : `If ${agent.runner.label} is still connected, its next call starts a new agent. Remove the connector there to stop it.`} It still
        counts toward your agent limit until a day after it was created.
      </p>
      {remove.isError && <Fault>Could not delete the agent. Try again in a moment.</Fault>}
      <Actions onClose={onClose}>
        <Button variant="danger" size="lg" disabled={remove.isPending} onClick={() => remove.mutate(undefined, { onSuccess: () => navigate({ to: "/" }) })}>
          Delete agent
        </Button>
      </Actions>
    </Sheet>
  );
};

const Contest = ({ agent, competition, other, now }: { agent: Agent; competition: RaceHead; other: Agent | undefined; now: number }) => {
  const entry = useEntry(agent.eth_address);
  const entered = agent.competitions.includes(competition.slug);
  const upcoming = competition.phase === "upcoming";
  const line = entered ? (upcoming ? undefined : "Entered") : other && (upcoming ? `Replaces ${other.name}` : `${other.name} is entered`);
  const action = entered ? (upcoming ? "Leave" : undefined) : upcoming || !other ? "Enter" : undefined;
  return (
    <li className="flex min-h-15 items-center justify-between gap-4 border-t border-line py-3">
      <div className="min-w-0">
        <p className="font-medium">{competition.title}</p>
        <p className="flex flex-wrap items-center gap-x-1.5 text-caption text-muted *:whitespace-nowrap">
          <span>{span(competition.startsAt, competition.endsAt)}</span>
          <Sep />
          <span>{stageText(competition, now)}</span>
          <Sep />
          <span>{pool(competition.prizes)}</span>
        </p>
        {line && <p className="text-caption text-muted">{line}</p>}
        {entry.isError && <Fault>{(entry.error instanceof ApiError && entry.error.detail) || "Could not update the entry. Try again in a moment."}</Fault>}
      </div>
      {action && (
        <Button variant={entered ? "secondary" : "primary"} disabled={entry.isPending} onClick={() => entry.mutate({ slug: competition.slug, enter: !entered })}>
          {action}
        </Button>
      )}
    </li>
  );
};

const ShareSheet = ({ line, url, image, onClose }: { line: string; url: string; image: string; onClose: () => void }) => (
  <Sheet title="Share the agent" onClose={onClose}>
    <div className="mt-4 grid gap-4">
      <p className="text-body text-pretty wrap-anywhere select-all">
        {line}. <span className="text-muted">{url}</span>
      </p>
      <img className="aspect-[1200/630] w-full rounded-item border border-line bg-surface" src={image} alt="" />
      <div className="flex gap-2">
        <Copy text={`${line}. ${url}`} variant="primary" size="lg" className="flex-1" />
        <Button href={`https://x.com/intent/post?${new URLSearchParams({ text: line, url })}`} size="lg" target="_blank" rel="noreferrer" className="flex-1">
          Post on X
        </Button>
      </div>
    </div>
  </Sheet>
);

const Compete = ({ agent, competitions, now, onClose }: { agent: Agent; competitions: readonly RaceHead[]; now: number; onClose: () => void }) => {
  const agents = useAgents().data ?? [];
  return (
    <Sheet title="Compete" onClose={onClose}>
      <ul className="mt-4">
        {competitions.map((competition) => (
          <Contest
            key={competition.slug}
            agent={agent}
            competition={competition}
            other={agents.find((owned) => owned.eth_address !== agent.eth_address && owned.competitions.includes(competition.slug))}
            now={now}
          />
        ))}
      </ul>
      <p className="mt-4 text-caption text-muted">
        Top-ups pause from 24 hours before the start until the final values are in.{" "}
        <a href={`${SITE}/competitions#how-it-works`} target="_blank" rel="noreferrer" className={UNDERLINE}>
          How it works
        </a>
      </p>
    </Sheet>
  );
};

const View = ({ agent, now }: { agent: Agent; now: number }) => {
  const [sheet, setSheet] = useState<"share" | "compete" | "rename" | "delete">();
  const [open, setOpen] = useState<string | null>(null);
  const competitions = useCompetitions().data?.competitions.filter(({ phase }) => phase !== "ended") ?? [];
  const entries = useRaceEntries(agent.eth_address).data ?? [];
  const { data: profile, isError } = useProfile(agent);
  const orders = useOrders(agent).data ?? [];
  const page = `${SITE}/agents/${agent.eth_address}`;
  const iso = new Date(now * 1000).toISOString();
  const bet = profile?.bets.find((candidate) => candidate.id === open);
  const close = () => setSheet(undefined);

  return (
    <>
      <Crumbs current={agent.name}>
        <div className="flex shrink-0 items-center gap-2">
          {profile && (
            <Button className={ICON} onClick={() => setSheet("share")}>
              <Share size={14} strokeWidth={1.75} />
              <span className="max-md:sr-only">Share</span>
            </Button>
          )}
          {agent.trades > 0 && (
            <Button href={page} target="_blank" rel="noreferrer" className={ICON}>
              <ArrowUpRight size={14} strokeWidth={1.75} />
              <span className="max-md:sr-only">Public page</span>
            </Button>
          )}
          {competitions.length > 0 && (
            <Button className={ICON} onClick={() => setSheet("compete")}>
              <Trophy size={14} strokeWidth={1.75} />
              <span className="max-md:sr-only">Compete</span>
            </Button>
          )}
          <Button className={ICON} onClick={() => setSheet("rename")}>
            <Pencil size={14} strokeWidth={1.75} />
            <span className="max-md:sr-only">Rename</span>
          </Button>
          <Button variant="warn" className={ICON} onClick={() => setSheet("delete")}>
            <Trash2 size={14} strokeWidth={1.75} />
            <span className="max-md:sr-only">Delete</span>
          </Button>
        </div>
      </Crumbs>
      <Header agent={agent} profile={profile} now={now} />
      <div className="mt-5 grid gap-5 sm:mt-7">
        {entries.length > 0 && (
          <article className={CARD}>
            <Label icon={Trophy} title="Competitions" />
            <ul className="mt-4">
              {entries.map((entry) => (
                <Entry key={entry.race.slug} entry={entry} now={now} />
              ))}
            </ul>
          </article>
        )}
        {profile ? (
          <>
            <EarnedCard profile={profile} />
            <Highlights bets={profile.bets} onOpen={setOpen} />
          </>
        ) : (
          (agent.trades === 0 || isError) && (
            <p className="text-muted">{agent.trades === 0 ? `${dollars(agent.equity)} of paper money is ready.` : "Could not load this agent. Reload to try again."}</p>
          )
        )}
        <BetsCard profile={profile} orders={orders} now={now} onOpen={setOpen} />
      </div>
      {profile && <Wallet address={profile.address} valuedAt={profile.valuedAt} />}
      {bet && <BetSheet bet={bet} now={now} onClose={() => setOpen(null)} />}
      {sheet === "share" && profile && <ShareSheet line={profile.share} url={`${page}?d=${iso.slice(0, 10)}`} image={`${page}/og/${iso.slice(0, 13)}.png`} onClose={close} />}
      {sheet === "compete" && <Compete agent={agent} competitions={competitions} now={now} onClose={close} />}
      {sheet === "rename" && <Rename agent={agent} onClose={close} />}
      {sheet === "delete" && <Delete agent={agent} onClose={close} />}
    </>
  );
};

export const AgentPage = () => {
  const { address } = route.useParams();
  const { data: agents, isError, dataUpdatedAt } = useAgents();
  const agent = agents?.find((candidate) => candidate.eth_address.toLowerCase() === address.toLowerCase());
  if (!agents) return isError ? <Note>Could not load this agent. Reload to try again.</Note> : null;
  if (!agent)
    return (
      <>
        <Crumbs current="Not found" />
        <Note>This agent was deleted or is not yours.</Note>
      </>
    );
  return <View key={agent.eth_address} agent={agent} now={dataUpdatedAt / 1000} />;
};
