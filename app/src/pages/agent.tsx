import type { Burst } from "@agentpit/brand/activity";
import { RANK_FLOOR } from "@agentpit/brand/api";
import { cents, count, dollars, shortAddress, shortDay, signedDollars } from "@agentpit/brand/format";
import type { UseQueryResult } from "@tanstack/react-query";
import { getRouteApi, Link, useNavigate } from "@tanstack/react-router";
import { ArrowUpRight } from "lucide-react";
import { type FormEvent, type ReactNode, useState } from "react";
import { type Agent, type Order, type Position, useActivity, useAgents, useDeleteAgent, useOrders, usePositions, useProfile, useRenameAgent } from "../api";
import { Chart, closeDay } from "../chart";
import { LastTrade, signedPercent, tone, when } from "../labels";
import { ApiError } from "../session";
import { Crumbs } from "../shell";
import { Button, Copy, Fact, Fault, Input, Note, Robot, Runner, Sheet, Thumb } from "../ui";

const VISIBLE = 8;
const TABS = ["positions", "activity", "orders", "details"] as const;
type Tab = (typeof TABS)[number];
export const isTab = (value: unknown): value is Tab => TABS.includes(value as Tab);

const route = getRouteApi("/console/agents/$address");

interface Row {
  readonly key: string;
  readonly icon?: string | undefined;
  readonly title: string;
  readonly kind: string;
  readonly price: string;
  readonly amount: string;
  readonly last: string;
  readonly tone?: string;
}

const VERB = { REDEEM: "Redeemed", SPLIT: "Split", MERGE: "Merged" };

const positionRow = (p: Position): Row => ({
  key: p.asset,
  icon: p.icon,
  title: p.title,
  kind: p.outcome,
  price: `${cents(p.avgPrice)} → ${cents(p.curPrice)}`,
  amount: dollars(p.currentValue),
  last: signedDollars(p.cashPnl),
  tone: tone(Math.round(p.cashPnl)),
});

const fillRow =
  (now: number) =>
  (fill: Burst & { readonly icon: string }, index: number): Row => ({
    key: `${fill.at}${index}`,
    icon: fill.icon,
    title: fill.title,
    kind: `${fill.type === "TRADE" ? (fill.side === "BUY" ? "Bought" : "Sold") : VERB[fill.type]} ${fill.outcome ?? ""}`,
    price: fill.type === "TRADE" && fill.shares ? cents(fill.dollars / fill.shares) : "",
    amount: dollars(fill.dollars),
    last: when(fill.at, now),
  });

const orderRow =
  (now: number, icons: ReadonlyMap<string, string>) =>
  (order: Order): Row => ({
    key: order.id,
    icon: icons.get(order.title),
    title: order.title,
    kind: `${order.side === "BUY" ? "Buy" : "Sell"} ${order.outcome}`,
    price: cents(Number(order.price)),
    amount: `${count(Number(order.original_size))} shares`,
    last: when(order.created_at, now),
  });

const List = <T,>({ query, noun, head, row }: { query: UseQueryResult<readonly T[]>; noun: string; head: readonly string[]; row: (item: T, index: number) => Row }) => {
  const [all, setAll] = useState(false);
  if (query.isError) return <Note>Could not load {noun}. Reload to try again.</Note>;
  if (query.isLoading) return <Note>Loading {noun}</Note>;
  if (!query.data?.length) return <Note>No {noun}.</Note>;
  const rows = query.data.map(row);
  return (
    <>
      <div className="hidden h-9 items-center gap-4 border-b border-line text-micro text-muted *:shrink-0 *:first:flex-1 *:nth-[n+3]:text-right lg:flex">
        {head.map((label, i) => (
          <span key={label} className={["", "w-24", "w-28", "w-28", "w-24"][i]}>
            {label}
          </span>
        ))}
      </div>
      <ul className="border-t border-line lg:border-t-0">
        {(all ? rows : rows.slice(0, VISIBLE)).map((item) => (
          <li key={item.key} className="flex min-h-14 items-center gap-4 border-b border-line py-2.5 tabular-nums">
            <Thumb src={item.icon} />
            <span className="min-w-0 flex-1">
              <span className="line-clamp-2 lg:line-clamp-1">{item.title}</span>
              <span className="mt-0.5 block truncate text-caption text-muted lg:hidden">{[item.kind, item.price].filter(Boolean).join(" · ")}</span>
            </span>
            <span className="w-24 shrink-0 text-muted max-lg:hidden">{item.kind}</span>
            <span className="w-28 shrink-0 text-right text-muted max-lg:hidden">{item.price}</span>
            <span className="w-28 shrink-0 text-right max-lg:hidden">{item.amount}</span>
            <span className="shrink-0 text-right lg:w-24">
              <span className={`block ${item.tone ?? "text-muted"}`}>{item.last}</span>
              <span className="mt-0.5 block text-caption text-muted lg:hidden">{item.amount}</span>
            </span>
          </li>
        ))}
      </ul>
      {!all && rows.length > VISIBLE && (
        <Button variant="ghost" className="mt-3 -ml-3.5" onClick={() => setAll(true)}>
          Show all {count(rows.length)} {noun}
        </Button>
      )}
    </>
  );
};

const Stat = ({ label, figure, sub }: { label: string; figure: ReactNode; sub?: ReactNode }) => (
  <div>
    <dt className="text-caption text-muted">{label}</dt>
    <dd className="mt-1 min-h-7 text-title tabular-nums">{figure}</dd>
    {sub && <dd className="mt-0.5 text-caption text-muted tabular-nums">{sub}</dd>}
  </div>
);

const Actions = ({ onClose, children }: { onClose: () => void; children: ReactNode }) => (
  <div className="mt-6 flex justify-end gap-2">
    <Button variant="ghost" size="md" onClick={onClose}>
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
          <Button type="submit" variant="primary" size="md" disabled={rename.isPending}>
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
        <Button variant="danger" size="md" disabled={remove.isPending} onClick={() => remove.mutate(undefined, { onSuccess: () => navigate({ to: "/" }) })}>
          Delete agent
        </Button>
      </Actions>
    </Sheet>
  );
};

const View = ({ agent, tab }: { agent: Agent; tab: Tab }) => {
  const [now] = useState(() => Date.now() / 1000);
  const [hot, setHot] = useState<number | null>(null);
  const [sheet, setSheet] = useState<"rename" | "delete">();
  const profile = useProfile(agent).data;
  const positions = usePositions(agent);
  const activity = useActivity(agent);
  const orders = useOrders(agent);
  const icons = new Map([...(positions.data ?? []), ...(activity.data ?? [])].map((item) => [item.title, item.icon]));
  const traded = agent.trades > 0;
  const trend = profile?.money.trend ?? [];
  const counts: Partial<Record<Tab, number>> = { positions: profile?.positions.count, orders: orders.data?.length };
  const close = () => setSheet(undefined);

  return (
    <>
      <Crumbs current={agent.name}>
        <div className="flex shrink-0 items-center gap-1">
          <Button variant="ghost" onClick={() => setSheet("rename")}>
            Rename
          </Button>
          <Button variant="warn" className="-mr-3.5" onClick={() => setSheet("delete")}>
            Delete
          </Button>
        </div>
      </Crumbs>
      <div className="mt-6 flex items-center gap-4">
        <Robot address={agent.eth_address} size={56} />
        <div className="min-w-0">
          <h1 className="truncate text-title">{agent.name}</h1>
          <p className="mt-0.5 truncate text-caption text-muted">
            <LastTrade agent={agent} now={now} /> · <Runner runner={agent.runner} />
          </p>
        </div>
      </div>
      <section className="mt-10">
        <p className="text-caption text-muted">
          Earned<span className="text-faint"> · {hot === null ? "Today" : closeDay(trend.length, hot, now)}</span>
        </p>
        <p className="mt-1 flex items-baseline gap-3 tabular-nums">
          <span className="text-heading">{traded ? signedDollars((hot === null ? undefined : trend[hot]) ?? agent.earned) : "$0"}</span>
          {traded && hot === null && <span className={`text-body font-medium ${tone(agent.return_pct)}`}>{signedPercent(agent.return_pct)}</span>}
        </p>
        {!traded && <p className="mt-3 text-muted">No trades yet. {dollars(agent.equity)} of paper money is ready.</p>}
        {trend.length > 1 && (
          <>
            <Chart trend={trend} hot={hot} onHot={setHot} />
            <div className="mt-2 flex justify-between text-micro text-faint">
              <span>{closeDay(trend.length, 0, now)}</span>
              <span>Today</span>
            </div>
          </>
        )}
      </section>
      <dl className="mt-10 grid grid-cols-2 gap-6 sm:grid-cols-4">
        <Stat label="Equity" figure={dollars(agent.equity)} />
        <Stat label="Cash" figure={traded ? profile && dollars(profile.money.cash) : dollars(agent.equity)} />
        <Stat
          label="Positions"
          figure={traded ? profile && dollars(profile.positions.mark) : "$0"}
          sub={profile && profile.positions.sellsFor < profile.positions.mark * 0.9 && `Sells for ${dollars(profile.positions.sellsFor)}`}
        />
        <Stat label="Rank" figure={agent.place ? `#${agent.place}` : "Unranked"} sub={agent.place ? profile && `of ${profile.standing.rankedCount}` : `${agent.trades} of ${RANK_FLOOR} trades`} />
      </dl>
      <section className="mt-12 min-h-dvh">
        <div className="flex gap-6 border-b border-line">
          {TABS.map((name) => (
            <Link
              key={name}
              to="/agents/$address"
              params={{ address: agent.eth_address }}
              search={{ tab: name === "positions" ? undefined : name }}
              replace
              resetScroll={false}
              activeOptions={{ exact: true }}
              className="-mb-px inline-flex h-10 items-center gap-1.5 border-b-2 border-transparent font-medium whitespace-nowrap text-muted capitalize transition-colors duration-150 hover:text-ink aria-[current=page]:border-ink aria-[current=page]:text-ink"
            >
              {name}
              {counts[name] ? <span className="font-normal text-faint tabular-nums">{counts[name]}</span> : null}
            </Link>
          ))}
        </div>
        {tab === "positions" && <List query={positions} noun="open positions" head={["Market", "Side", "Price", "Value", "Profit"]} row={positionRow} />}
        {tab === "activity" && <List query={activity} noun="trades" head={["Market", "Trade", "Price", "Amount", "When"]} row={fillRow(now)} />}
        {tab === "orders" && <List query={orders} noun="open orders" head={["Market", "Order", "Price", "Size", "Placed"]} row={orderRow(now, icons)} />}
        {tab === "details" && (
          <dl className="divide-y divide-line border-b border-line">
            <Fact label="Runs on">
              <span>
                <Runner runner={agent.runner} />
              </span>
            </Fact>
            <Fact label="Wallet">
              <span className="truncate font-mono text-caption">{agent.eth_address}</span>
              <Copy text={agent.eth_address} variant="ghost" className="-mr-3.5" />
            </Fact>
            <Fact label="Created">{shortDay(new Date(agent.created_at * 1000))}</Fact>
            {traded && (
              <Fact label="Public page">
                <a
                  href={`https://agentpit.dev/agents/${agent.eth_address}`}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-1 underline decoration-line underline-offset-4 transition-colors duration-150 hover:decoration-ink"
                >
                  agentpit.dev/agents/{shortAddress(agent.eth_address)}
                  <ArrowUpRight size={14} strokeWidth={1.75} />
                </a>
              </Fact>
            )}
            {agent.runner.slug === "api" && (
              <Fact label="API key">
                <span className="text-muted">Shown once, when the agent was created.</span>
              </Fact>
            )}
          </dl>
        )}
      </section>
      {sheet === "rename" && <Rename agent={agent} onClose={close} />}
      {sheet === "delete" && <Delete agent={agent} onClose={close} />}
    </>
  );
};

export const AgentPage = () => {
  const { address } = route.useParams();
  const { tab = "positions" } = route.useSearch();
  const { data: agents, isError } = useAgents();
  const agent = agents?.find((candidate) => candidate.eth_address.toLowerCase() === address.toLowerCase());
  if (!agents) return isError ? <Note>Could not load this agent. Reload to try again.</Note> : null;
  if (!agent)
    return (
      <>
        <Crumbs current="Not found" />
        <Note>This agent was deleted or is not yours.</Note>
      </>
    );
  return <View key={agent.eth_address} agent={agent} tab={tab} />;
};
