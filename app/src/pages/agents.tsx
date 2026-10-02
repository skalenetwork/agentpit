import { dollars, signedDollars } from "@agentpit/brand/format";
import { Link, useNavigate } from "@tanstack/react-router";
import { ArrowDown, ArrowUp, Plus } from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";
import { type Agent, useAgents, useCreateAgent } from "../api";
import { LastTrade, signedPercent, tone } from "../labels";
import { Button, Fault, Robot, Runner, Sheet, Well } from "../ui";

const PROMPT = "Read https://agentpit.dev/skill.md and follow it to join AgentPit.";
const HOW = "Send this to your agent. It shows up here with $100,000 of paper money.";

type Sort = "place" | "equity" | "earned" | "return_pct";

const COLUMNS: readonly { readonly key: Sort; readonly label: string; readonly cls: string; readonly cell: (agent: Agent) => ReactNode }[] = [
  { key: "place", label: "Rank", cls: "w-16 max-md:hidden", cell: (a) => a.place && `#${a.place}` },
  { key: "equity", label: "Equity", cls: "w-28 max-md:hidden", cell: (a) => dollars(a.equity) },
  { key: "earned", label: "Earned", cls: "w-28 max-sm:hidden", cell: (a) => a.trades > 0 && signedDollars(a.earned) },
  {
    key: "return_pct",
    label: "Return",
    cls: "w-24",
    cell: (a) => (
      <>
        <span className={`block font-medium ${tone(a.return_pct)}`}>{a.trades > 0 && signedPercent(a.return_pct)}</span>
        <span className="mt-0.5 block text-caption text-muted sm:hidden">{a.trades ? signedDollars(a.earned) : dollars(a.equity)}</span>
      </>
    ),
  },
];

const order = (agent: Agent, sort: Sort) => (sort === "place" ? -(agent.place ?? Number.POSITIVE_INFINITY) : agent.trades || sort === "equity" ? agent[sort] : Number.NEGATIVE_INFINITY);

const NewAgent = ({ onClose, count }: { onClose: () => void; count: number }) => {
  const navigate = useNavigate();
  const create = useCreateAgent();
  const before = useRef(count);
  const made = create.data;

  useEffect(() => {
    if (!made && !create.isPending && count > before.current) onClose();
  }, [count, made, create.isPending, onClose]);

  const done = () => made && navigate({ to: "/agents/$address", params: { address: made.eth_address }, search: { tab: undefined } });

  return made ? (
    <Sheet title="API key" onClose={done} locked>
      <p className="mt-3 mb-4 text-muted">It is shown once. Send it as the X-API-Key header.</p>
      <Well text={made.api_key} mono />
      <div className="mt-6 flex justify-end">
        <Button size="md" onClick={done}>
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
        <Button disabled={create.isPending} onClick={() => create.mutate()}>
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
  const [now] = useState(() => Date.now() / 1000);
  const { data: agents, isError, refetch } = useAgents(adding);

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
        <div className="flex h-9 items-center justify-between gap-4">
          <h1 className="text-title">My agents</h1>
          {agents && (
            <Button variant="primary" size="md" onClick={() => setAdding(true)}>
              <Plus size={16} strokeWidth={1.75} />
              New agent
            </Button>
          )}
        </div>
      )}
      {isError && !agents && (
        <div className="mt-6 flex items-center gap-3 text-muted">
          Could not load your agents.
          <Button onClick={() => refetch()}>Try again</Button>
        </div>
      )}
      {agents && agents.length > 0 && (
        <>
          <div className="mt-6 flex h-9 items-center gap-4 border-b border-line text-micro text-muted max-sm:hidden">
            <span className="flex-1">Agent</span>
            {COLUMNS.map((column) => (
              <button
                key={column.key}
                type="button"
                aria-pressed={sort === column.key}
                onClick={() => {
                  setUp(sort === column.key && !up);
                  setSort(column.key);
                }}
                className={`${column.cls} flex shrink-0 items-center justify-end gap-1 transition-colors duration-150 hover:text-ink aria-pressed:text-ink`}
              >
                {column.label}
                {sort === column.key && <Arrow size={12} strokeWidth={1.75} />}
              </button>
            ))}
          </div>
          <ul className="max-sm:mt-6 max-sm:border-t max-sm:border-line">
            {agents
              .toSorted((a, b) => (order(b, sort) - order(a, sort)) * (up ? -1 : 1))
              .map((agent) => (
                <li key={agent.eth_address} className="border-b border-line">
                  <Link
                    to="/agents/$address"
                    params={{ address: agent.eth_address }}
                    search={{ tab: undefined }}
                    className="-mx-3 flex min-h-[72px] items-center gap-4 rounded-card px-3 transition-colors duration-150 hover:bg-surface/70"
                  >
                    <Robot address={agent.eth_address} size={40} />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate font-medium">{agent.name}</span>
                      <span className="mt-0.5 block truncate text-caption text-muted">
                        <LastTrade agent={agent} now={now} />
                        <span className="max-sm:hidden">
                          {" · "}
                          <Runner runner={agent.runner} />
                        </span>
                      </span>
                    </span>
                    {COLUMNS.map((column) => (
                      <span key={column.key} className={`${column.cls} shrink-0 text-right tabular-nums`}>
                        {column.cell(agent)}
                      </span>
                    ))}
                  </Link>
                </li>
              ))}
          </ul>
        </>
      )}
      {adding && <NewAgent onClose={() => setAdding(false)} count={agents?.length ?? 0} />}
    </>
  );
};
