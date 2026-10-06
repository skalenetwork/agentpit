import { bursts } from "@agentpit/brand/activity";
import { type Fill, type MarketContext, marketCaption, type Profile } from "@agentpit/brand/api";
import { closeLabel, earnedToday, recordBars } from "@agentpit/brand/chart";
import { cents, count, dollars, pct, shortAddress, shortDay, signedDollars, tone, utcTime } from "@agentpit/brand/format";
import polymarket from "@agentpit/brand/polymarket.svg";
import { recordCalls } from "@agentpit/brand/standing";
import { getRouteApi, useNavigate } from "@tanstack/react-router";
import { ArrowUpRight, ChartLine, Pencil, Share, Trash2 } from "lucide-react";
import { type FormEvent, Fragment, type ReactNode, useState } from "react";
import { type Agent, type Order, useAgents, useDeleteAgent, useOrders, useProfile, useRenameAgent } from "../api";
import { Chart } from "../chart";
import { LastTrade, Sep, TONE } from "../labels";
import { ApiError } from "../session";
import { Crumbs } from "../shell";
import { Thumb } from "../thumb";
import { Button, Card, Copy, Fault, Input, Note, RankPill, Robot, Sheet, SOFT } from "../ui";

const SITE = "https://agentpit.dev/agents";
const EXPLORER = "https://skale-base-explorer.skalenodes.com/address";
const POSITIONS = 6;
const BURSTS = 8;
const DAY = 86_400;
const VERB = { BUY: "Bought", SELL: "Sold", SPLIT: "Split", MERGE: "Merged", REDEEM: "Redeemed" } as const;
const ICON = "max-sm:w-7.5 max-sm:px-0";
const LEAD = "flex min-h-10 items-center text-caption text-muted";
const POSITION_COLS = "md:grid-cols-[30px_minmax(0,1fr)_104px_104px_104px_104px]";
const BURST_COLS = "md:grid-cols-[30px_minmax(0,1fr)_160px_104px_104px]";

const route = getRouteApi("/console/agents/$address");

type Market = Pick<Fill, "title" | "icon" | "category" | "url"> & MarketContext;

interface Tab {
  readonly label: string;
  readonly count?: number;
  readonly panel: ReactNode;
}

interface RowProps {
  readonly market: Market;
  readonly now: number;
  readonly cols: string;
  readonly what: ReactNode;
  readonly at: ReactNode;
  readonly value?: ReactNode;
  readonly note?: ReactNode;
  readonly earned?: number;
}

const day = (at: number, now: number) => {
  const back = Math.floor(now / DAY) - Math.floor(at / DAY);
  return back === 0 ? "Today" : back === 1 ? "Yesterday" : shortDay(new Date(at * 1000));
};

const TINT: Readonly<Record<string, string>> = { Yes: "bg-yes/10 text-yes dark:bg-yes/16", No: "bg-no/10 text-no dark:bg-no/16" };

const Outcome = ({ outcome }: { outcome: string }) => (
  <span className={`inline-flex h-6 shrink-0 items-center rounded-control px-2.5 text-label font-medium ${TINT[outcome] ?? "bg-paper text-ink"}`}>{outcome}</span>
);

const Move = ({ from, to }: { from: number; to: number }) => (
  <span>
    {from ? cents(from) : "Split"} <span className="text-faint">→</span> {cents(to)}
  </span>
);

const More = ({ onClick, children }: { onClick: () => void; children: ReactNode }) => (
  <div className="mt-3 flex">
    <Button variant="paper" onClick={onClick}>
      {children}
    </Button>
  </div>
);

const Caption = ({ parts }: { parts: readonly string[] }) =>
  parts.length > 0 && (
    <span className="flex min-w-0 items-center gap-1.5 overflow-hidden text-caption whitespace-nowrap text-muted">
      {parts.map((part, i) => (
        <Fragment key={part}>
          {i > 0 && <Sep />}
          <span className={i ? "shrink-0" : "truncate"}>{part}</span>
        </Fragment>
      ))}
    </span>
  );

const Row = ({ market, now, cols, what, at, value, note, earned }: RowProps) => (
  <li className={`grid grid-cols-[30px_minmax(0,1fr)_auto] items-start gap-x-3 border-t border-line py-2.5 tabular-nums md:items-center md:py-2 ${note ? "md:min-h-15" : "md:min-h-13"} ${cols}`}>
    <Thumb src={market.icon} title={market.title} category={market.category} />
    <span className="min-w-0 md:contents">
      <span className="grid min-w-0">
        {market.url ? (
          <a href={market.url} target="_blank" rel="noopener" className="group/link flex max-w-full min-w-0 items-end gap-1 justify-self-start font-medium">
            <span className="min-w-0 underline-offset-2 group-hover/link:underline max-md:line-clamp-2 md:truncate">{market.title}</span>
            <span
              aria-hidden="true"
              className="mb-1 size-3 shrink-0 bg-muted transition-colors duration-150 group-hover/link:bg-ink"
              style={{ mask: `url("${polymarket}") center / contain no-repeat` }}
            />
            <span className="sr-only">, on Polymarket</span>
          </a>
        ) : (
          <span className="line-clamp-2 font-medium md:line-clamp-1">{market.title}</span>
        )}
        <Caption parts={marketCaption(market, now)} />
      </span>
      <span className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-muted max-md:text-caption md:contents">
        <span className="flex items-center gap-1.5 whitespace-nowrap">{what}</span>
        <span className="whitespace-nowrap md:text-right">{at}</span>
      </span>
    </span>
    <span className="grid justify-items-end gap-0.5 text-right md:contents">
      {value !== undefined && (
        <span className="grid justify-items-end">
          {value}
          {note && <small className="mt-0.5 text-caption whitespace-nowrap text-muted">{note}</small>}
        </span>
      )}
      {earned !== undefined && <span className={`font-medium max-md:text-caption md:text-right ${TONE[tone(Math.round(earned))]}`}>{signedDollars(earned)}</span>}
    </span>
  </li>
);

const Tabs = ({ tabs }: { tabs: readonly Tab[] }) => {
  const [chosen, setChosen] = useState(tabs[0]?.label);
  const current = tabs.find((tab) => tab.label === chosen) ?? tabs[0];
  return (
    <section className={`px-5 pb-5 sm:px-6 sm:pb-6 ${SOFT}`}>
      <div role="tablist" className="-mx-5 flex border-b border-line px-2 sm:-mx-6 sm:px-3">
        {tabs.map((tab) => (
          <button
            key={tab.label}
            type="button"
            role="tab"
            aria-selected={tab === current}
            onClick={() => setChosen(tab.label)}
            className="group relative flex h-12 items-center font-medium text-muted outline-none transition-colors duration-150 after:absolute after:inset-x-3 after:-bottom-px after:h-0.5 after:rounded-full after:bg-ink after:opacity-0 hover:text-ink aria-selected:text-ink aria-selected:after:opacity-100"
          >
            <span className="flex h-8 items-center rounded-control px-3 transition-colors duration-150 group-hover:bg-paper group-focus-visible:outline-2 group-focus-visible:outline-offset-2 group-focus-visible:outline-signal">
              {tab.label}
              {tab.count !== undefined && <span className="ml-1 font-normal text-muted tabular-nums max-sm:hidden">{count(tab.count)}</span>}
            </span>
          </button>
        ))}
      </div>
      <div role="tabpanel">{current?.panel}</div>
    </section>
  );
};

const Header = ({ agent, profile, now }: { agent: Agent; profile: Profile | undefined; now: number }) => (
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
              <span>Trading since {shortDay(new Date(profile.firstTradeAt * 1000))}</span>
              <Sep />
              <LastTrade at={profile.lastTradeAt} now={now} prefix="Last trade " />
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
        {(
          [
            ["Return", pct(profile.money.returnPct), TONE[tone(profile.money.returnPct)], 0],
            ["Earned", signedDollars(profile.money.earned), TONE[tone(Math.round(profile.money.earned))], Math.round(earnedToday(profile.money.earned, profile.money.trend))],
            ["Equity", dollars(profile.money.capital), "text-ink", 0],
          ] as const
        ).map(([term, value, color, today]) => (
          <div key={term} className="grid min-w-0 content-start sm:justify-items-end sm:text-right">
            <dt className="text-caption text-muted">{term}</dt>
            <dd className={`truncate text-figure tabular-nums max-sm:text-title ${color}`}>{value}</dd>
            {today !== 0 && (
              <dd className="text-caption whitespace-nowrap text-muted tabular-nums">
                <span className={TONE[tone(today)]}>{signedDollars(today)}</span> today
              </dd>
            )}
          </div>
        ))}
      </dl>
    )}
  </header>
);

const EarnedCard = ({ money, valuedAt }: { money: Profile["money"]; valuedAt: number }) => {
  const [hot, setHot] = useState<number | null>(null);
  const { trend, trendStart } = money;
  const value = hot === null ? undefined : trend[hot];
  return (
    <Card
      title="Earned, last 30 days"
      icon={ChartLine}
      note={
        hot === null || value === undefined ? (
          `As of ${utcTime(new Date(valuedAt * 1000))}`
        ) : (
          <>
            {closeLabel(trendStart, hot, trend.length)} <span className={TONE[tone(Math.round(value))]}>{signedDollars(value)}</span>
          </>
        )
      }
    >
      {trend.length > 1 ? (
        <>
          <Chart trend={trend} hot={hot} onHot={setHot} />
          <p className="mt-2 flex justify-between text-caption text-muted tabular-nums">
            <span>{closeLabel(trendStart, 0, trend.length)}</span>
            <span>Today</span>
          </p>
        </>
      ) : (
        <p className="text-muted">The line starts after the first UTC close.</p>
      )}
    </Card>
  );
};

const PositionsPanel = ({ positions, cash, now }: { positions: Profile["positions"]; cash: number; now: number }) => {
  const [all, setAll] = useState(false);
  const { open } = positions;
  return (
    <>
      <div className={`${LEAD} gap-x-3 md:grid ${POSITION_COLS}`}>
        <p className="flex flex-wrap items-center gap-x-1.5 md:col-span-3">
          <span>
            <span className="font-medium text-ink tabular-nums">{dollars(positions.mark)}</span> in positions
          </span>
          <Sep />
          <span>
            <span className="font-medium text-ink tabular-nums">{dollars(cash)}</span> cash
          </span>
        </p>
        {open.length > 0 &&
          ["Avg → now", "Value", "Earned"].map((head) => (
            <span key={head} className="text-right max-md:hidden">
              {head}
            </span>
          ))}
      </div>
      {open.length ? (
        <ul>
          {(all ? open : open.slice(0, POSITIONS)).map((position) => (
            <Row
              key={`${position.title}${position.outcome}`}
              market={position}
              now={now}
              cols={POSITION_COLS}
              what={<Outcome outcome={position.outcome} />}
              at={<Move from={position.avgPrice} to={position.curPrice} />}
              value={dollars(position.value)}
              note={position.sellsFor < 0.9 * position.value && `sells for ${dollars(position.sellsFor)}`}
              earned={position.pnl}
            />
          ))}
        </ul>
      ) : (
        <p className="flex min-h-13 items-center border-t border-line text-muted">Nothing open.</p>
      )}
      {!all && open.length > POSITIONS && <More onClick={() => setAll(true)}>Show all {count(open.length)} positions</More>}
    </>
  );
};

const ActivityPanel = ({ activity, now }: { activity: Profile["activity"]; now: number }) => {
  const [all, setAll] = useState(false);
  const list = bursts(activity);
  const shown = all ? list : list.slice(0, BURSTS);
  return (
    <>
      {[...new Set(shown.map((burst) => day(burst.at, now)))].map((label) => (
        <section key={label} className="not-first:mt-3">
          <h3 className={LEAD}>{label}</h3>
          <ul>
            {shown
              .filter((burst) => day(burst.at, now) === label)
              .map((burst) => (
                <Row
                  key={`${burst.at}${burst.title}${burst.outcome}`}
                  market={burst}
                  now={now}
                  cols={BURST_COLS}
                  what={
                    <>
                      <span className="text-ink">{burst.type === "TRADE" ? VERB[burst.side ?? "BUY"] : VERB[burst.type]}</span>
                      {burst.outcome && <Outcome outcome={burst.outcome} />}
                      {burst.n > 1 && <span>×{burst.n}</span>}
                    </>
                  }
                  at={burst.type === "TRADE" && burst.shares > 0 && `at ${cents(burst.dollars / burst.shares)}`}
                  value={dollars(burst.dollars)}
                />
              ))}
          </ul>
        </section>
      ))}
      {!all && list.length > BURSTS && <More onClick={() => setAll(true)}>Show {count(list.length - BURSTS)} more</More>}
    </>
  );
};

const RecordPanel = ({ record, now }: { record: NonNullable<Profile["record"]>; now: number }) => {
  const decided = record.wins + record.losses;
  const { zero, width, bars } = recordBars(record.pnls);
  const won = bars.filter((bar) => bar.tone === "up").length;
  return (
    <>
      <p className={LEAD}>
        <span>
          <span className="font-medium text-ink tabular-nums">{record.wins}</span> of <span className="font-medium text-ink tabular-nums">{decided}</span> won
        </span>
      </p>
      {decided >= 3 && (
        <>
          <svg className="mt-1 h-22 w-full overflow-visible" role="img" aria-label={`${won} won, ${bars.length - won} lost, oldest to latest`}>
            <line x2="100%" y1={`${zero}%`} y2={`${zero}%`} className="stroke-line" strokeDasharray="3 3" />
            {bars.map((bar) => (
              <rect key={bar.x} x={`${bar.x}%`} y={`${bar.y}%`} width={`${width}%`} height={`${bar.height}%`} rx="2" className={bar.tone === "up" ? "fill-up" : "fill-down"} />
            ))}
          </svg>
          <p className="mt-2 mb-3 flex justify-between text-caption leading-4 text-muted">
            <span>Oldest</span>
            <span>Latest</span>
          </p>
        </>
      )}
      <ul>
        {recordCalls(record).map(({ label, call }) => (
          <Row
            key={label}
            market={call}
            now={now}
            cols={BURST_COLS}
            what={
              <>
                <span className="text-ink">{label}</span>
                <Outcome outcome={call.outcome} />
              </>
            }
            at={<Move from={call.entry} to={call.exit} />}
            earned={call.pnl}
          />
        ))}
      </ul>
    </>
  );
};

const OrdersPanel = ({ orders, icons, now }: { orders: readonly Order[]; icons: ReadonlyMap<string, string | null>; now: number }) => (
  <>
    <p className={LEAD}>Resting orders fill when the Polymarket book reaches the price.</p>
    <ul>
      {orders.map((order) => (
        <Row
          key={order.id}
          market={{ ...order, icon: icons.get(order.title) ?? null, category: null }}
          now={now}
          cols={BURST_COLS}
          what={
            <>
              <span className="text-ink">{order.side === "BUY" ? "Buy" : "Sell"}</span>
              <Outcome outcome={order.outcome} />
            </>
          }
          at={`${count(Number(order.original_size))} at ${cents(Number(order.price))}`}
          value={dollars(Number(order.price) * Number(order.original_size))}
        />
      ))}
    </ul>
  </>
);

const Wallet = ({ address }: { address: string }) => (
  <footer className="mt-5 flex flex-wrap items-center gap-x-4 gap-y-1 text-caption text-muted">
    <span className="flex items-center gap-2">
      Wallet
      <span className="font-mono text-ink">{shortAddress(address)}</span>
      <Copy text={address} variant="ghost" size="icon" className="-ml-1" />
    </span>
    <a href={`${EXPLORER}/${address}`} target="_blank" rel="noreferrer" className="flex items-center gap-1 whitespace-nowrap transition-colors duration-150 hover:text-ink">
      View on explorer
      <ArrowUpRight size={12} strokeWidth={1.75} />
    </a>
    <span className="sm:ml-auto">Paper apUSD on SKALE on Base</span>
  </footer>
);

const Actions = ({ onClose, children }: { onClose: () => void; children: ReactNode }) => (
  <div className="mt-6 flex justify-end gap-2">
    <Button variant="ghost" size="lg" onClick={onClose}>
      Cancel
    </Button>
    {children}
  </div>
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
        {agent.runner.slug === "api" ? "Its API key stops working." : `If ${agent.runner.label} is still connected, its next call starts a new agent. Remove the connector there to stop it.`}
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

const View = ({ agent, now }: { agent: Agent; now: number }) => {
  const [sheet, setSheet] = useState<"rename" | "delete">();
  const { data: profile, isError } = useProfile(agent);
  const orders = useOrders(agent).data;
  const page = `${SITE}/${agent.eth_address}`;
  const icons = new Map([...(profile?.positions.open ?? []), ...(profile?.activity ?? [])].map((item) => [item.title, item.icon]));
  const ordersTab: readonly Tab[] = orders?.length ? [{ label: "Orders", count: orders.length, panel: <OrdersPanel orders={orders} icons={icons} now={now} /> }] : [];
  const close = () => setSheet(undefined);

  return (
    <>
      <Crumbs current={agent.name}>
        <div className="flex shrink-0 items-center gap-2">
          {profile && (
            <Button
              href={`https://x.com/intent/post?${new URLSearchParams({ text: profile.share, url: `${page}?d=${new Date(now * 1000).toISOString().slice(0, 10)}` })}`}
              target="_blank"
              rel="noreferrer"
              className={ICON}
            >
              <Share size={14} strokeWidth={1.75} />
              <span className="max-sm:sr-only">Share</span>
            </Button>
          )}
          {agent.trades > 0 && (
            <Button href={page} target="_blank" rel="noreferrer" className={ICON}>
              <ArrowUpRight size={14} strokeWidth={1.75} />
              <span className="max-sm:sr-only">Public page</span>
            </Button>
          )}
          <Button className={ICON} onClick={() => setSheet("rename")}>
            <Pencil size={14} strokeWidth={1.75} />
            <span className="max-sm:sr-only">Rename</span>
          </Button>
          <Button variant="warn" className={ICON} onClick={() => setSheet("delete")}>
            <Trash2 size={14} strokeWidth={1.75} />
            <span className="max-sm:sr-only">Delete</span>
          </Button>
        </div>
      </Crumbs>
      <Header agent={agent} profile={profile} now={now} />
      {profile ? (
        <>
          <div className="mt-5 grid gap-5 sm:mt-7">
            <EarnedCard money={profile.money} valuedAt={profile.valuedAt} />
            <Tabs
              tabs={[
                { label: "Positions", count: profile.positions.count, panel: <PositionsPanel positions={profile.positions} cash={profile.money.cash} now={now} /> },
                { label: "Activity", panel: <ActivityPanel activity={profile.activity} now={now} /> },
                ...(profile.record ? [{ label: "Record", count: profile.record.wins + profile.record.losses, panel: <RecordPanel record={profile.record} now={now} /> }] : []),
                ...ordersTab,
              ]}
            />
          </div>
          <Wallet address={profile.address} />
        </>
      ) : (
        <>
          {(agent.trades === 0 || isError) && (
            <p className="mt-5 text-muted sm:mt-7">{agent.trades === 0 ? `${dollars(agent.equity)} of paper money is ready.` : "Could not load this agent. Reload to try again."}</p>
          )}
          {ordersTab.length > 0 && (
            <div className="mt-5 sm:mt-7">
              <Tabs tabs={ordersTab} />
            </div>
          )}
        </>
      )}
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
