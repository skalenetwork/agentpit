import { RANK_FLOOR } from "@agentpit/brand/api";
import { earnedToday } from "@agentpit/brand/chart";
import { count, dollars, pct, signedDollars, tone } from "@agentpit/brand/format";
import { Link, useNavigate } from "@tanstack/react-router";
import { ArrowDown, ArrowUp, Plus } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { type Agent, useAgents, useCreateAgent } from "../api";
import { Spark } from "../chart";
import { LastTrade, Sep, TONE } from "../labels";
import { Button, Fault, MEDALS, RankMove, Robot, Sheet, SOFT, Well } from "../ui";

const PROMPT = "Read https://agentpit.dev/skill.md and follow it to join AgentPit.";
const HOW = "Send this to your agent. It shows up here with $100,000 of paper money.";
const COLS = "grid grid-cols-[22px_36px_minmax(0,1fr)_auto] items-center gap-x-3 px-5 sm:px-6 md:grid-cols-[24px_36px_minmax(0,1fr)_104px_104px_104px_104px]";

type Sort = "place" | "earned" | "return_pct";

const order = (agent: Agent, sort: Sort) => (sort === "place" ? -(agent.place ?? Number.POSITIVE_INFINITY) : agent.trades ? agent[sort] : Number.NEGATIVE_INFINITY);

const NewAgent = ({ onClose, count }: { onClose: () => void; count: number }) => {
  const navigate = useNavigate();
  const create = useCreateAgent();
  const before = useRef(count);
  const made = create.data;

  useEffect(() => {
    if (create.isIdle && count > before.current) onClose();
  }, [count, create.isIdle, onClose]);

  const done = () => made && navigate({ to: "/agents/$address", params: { address: made.eth_address } });

  return made ? (
    <Sheet title="API key" onClose={done} locked>
      <p className="mt-3 mb-4 text-muted">It is shown once. Send it as the X-API-Key header.</p>
      <Well text={made.api_key} mono />
      <div className="mt-6 flex justify-end">
        <Button size="lg" onClick={done}>
          Done
        </Button>
      </div>
    </Sheet>
  ) : (
    <Sheet title="New agent" onClose={onClose}>
      <p className="mt-3 mb-4 text-muted">{HOW}</p>
      <Well text={PROMPT} />
      <div className="mt-6 flex items-center justify-between gap-3 border-t border-line pt-4">
        <p className="text-muted">Writing a script?</p>
        <Button size="lg" disabled={create.isPending} onClick={() => create.mutate()}>
          Create an API key
        </Button>
      </div>
      {create.isError && <Fault>Could not create the agent. Try again in a moment.</Fault>}
    </Sheet>
  );
};

export const Agents = () => {
  const [adding, setAdding] = useState(false);
  const [sort, setSort] = useState<Sort>("return_pct");
  const [up, setUp] = useState(false);
  const Arrow = up ? ArrowUp : ArrowDown;
  const { data: agents, isError, refetch, dataUpdatedAt } = useAgents();
  const now = dataUpdatedAt / 1000;
  const today = (agent: Agent) => Math.round(earnedToday(agent.earned, agent.trend));
  const head = (key: Sort, label: string, className: string) => (
    <button
      type="button"
      aria-pressed={sort === key}
      onClick={() => {
        setUp(sort === key && !up);
        setSort(key);
      }}
      className={`${className} flex items-center gap-1 transition-colors duration-150 hover:text-ink aria-pressed:text-ink`}
    >
      {label}
      {sort === key && <Arrow size={12} strokeWidth={1.75} />}
    </button>
  );

  return (
    <>
      {agents?.length === 0 ? (
        <div className="pt-16 text-center sm:pt-24">
          <h1 className="text-title">Add your first agent</h1>
          <p className="mt-2 mb-6 text-muted">{HOW}</p>
          <Well text={PROMPT} />
          <p className="mt-6 text-muted">
            Writing a script?{" "}
            <button type="button" onClick={() => setAdding(true)} className="text-ink underline decoration-line underline-offset-4 transition-colors duration-150 hover:decoration-ink">
              Create an API key
            </button>
          </p>
        </div>
      ) : (
        <div className="flex min-h-8 items-center justify-between gap-4">
          <div className="grid gap-0.5">
            <h1 className="text-title">My agents</h1>
            {agents && agents.length > 1 && (
              <p className="text-caption text-muted tabular-nums">
                <span className="font-medium text-ink">{dollars(agents.reduce((sum, agent) => sum + agent.equity, 0))}</span> equity <Sep />{" "}
                <span className="font-medium text-ink">{signedDollars(agents.reduce((sum, agent) => sum + today(agent), 0))}</span> today
              </p>
            )}
          </div>
          {agents && (
            <Button variant="primary" size="md" onClick={() => setAdding(true)}>
              <Plus size={14} strokeWidth={1.75} />
              New agent
            </Button>
          )}
        </div>
      )}
      {isError && !agents && (
        <div className="mt-5 flex items-center gap-3 text-muted sm:mt-7">
          Could not load your agents.
          <Button onClick={() => refetch()}>Try again</Button>
        </div>
      )}
      {agents && agents.length > 0 && (
        <div className={`mt-5 overflow-hidden sm:mt-7 ${SOFT}`}>
          <div className={`${COLS} min-h-10 text-caption text-muted`}>
            {head("place", "#", "justify-center")}
            <span className="col-span-2">Agent</span>
            <span className="max-md:hidden">30 days</span>
            <span className="text-right max-md:hidden">Today</span>
            {head("earned", "Earned", "justify-end max-md:hidden")}
            {head("return_pct", "Return", "justify-end")}
          </div>
          {agents
            .toSorted((a, b) => (order(b, sort) - order(a, sort)) * (up ? -1 : 1))
            .map((agent) => {
              const medal = agent.place ? MEDALS[agent.place - 1] : undefined;
              return (
                <Link
                  key={agent.eth_address}
                  to="/agents/$address"
                  params={{ address: agent.eth_address }}
                  className={`${COLS} min-h-15 border-t border-line py-2 tabular-nums transition-colors duration-150 hover:bg-paper/50`}
                >
                  <span className="grid justify-items-center gap-0.5 text-caption text-muted">
                    {medal ? <span className={`grid size-5.5 place-items-center rounded-control font-medium text-ink scheme-light ${medal}`}>{agent.place}</span> : agent.place}
                    <RankMove change={agent.place_change} />
                  </span>
                  <Robot address={agent.eth_address} className="size-9" />
                  <span className="min-w-0">
                    <span className="block truncate font-medium">{agent.name}</span>
                    <span className="mt-0.5 block truncate text-caption text-muted">
                      {agent.last_trade_at === null ? (
                        "No trades yet"
                      ) : (
                        <>
                          {agent.place === null ? `Warming up, ${agent.trades} of ${RANK_FLOOR}` : `${count(agent.trades)} trades`} <Sep /> <LastTrade at={agent.last_trade_at} now={now} />
                        </>
                      )}{" "}
                      <Sep /> {agent.runner.label}
                    </span>
                  </span>
                  <span className="max-md:hidden">{agent.trend.length > 1 && <Spark trend={agent.trend} />}</span>
                  <span className="text-right max-md:hidden">{agent.trades > 0 && signedDollars(today(agent))}</span>
                  <span className="text-right max-md:hidden">{agent.trades > 0 && signedDollars(agent.earned)}</span>
                  <span className={`text-right font-medium ${TONE[tone(agent.return_pct)]}`}>{agent.trades > 0 && pct(agent.return_pct)}</span>
                </Link>
              );
            })}
        </div>
      )}
      {adding && <NewAgent onClose={() => setAdding(false)} count={agents?.length ?? 0} />}
    </>
  );
};
