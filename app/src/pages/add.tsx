import { Link, useNavigate } from "@tanstack/react-router";
import { Pencil } from "lucide-react";
import { type ComponentProps, type FormEvent, type ReactNode, useState } from "react";
import { type NewAgent, useAgents, useCreateAgent, useRenameAgent } from "../api";
import { ApiError } from "../session";
import { Button, Copy, Input, Robot, RunnerMark, Sheet, SOFT, Well } from "../ui";

const PROMPT = "Read https://agentpit.dev/skill.md and follow it to join AgentPit.";
const MCP = "https://api.agentpit.dev/mcp";
const RULE = /^[A-Za-z0-9_]{1,15}$/;

type Mark = ComponentProps<typeof RunnerMark>;

const AGENTS: readonly Mark[] = [
  { slug: "claude-code", label: "Claude Code" },
  { slug: "codex", label: "Codex" },
  { slug: "openclaw", label: "OpenClaw" },
  { slug: "hermes", label: "Hermes Agent" },
];
const APPS: readonly Mark[] = [
  { slug: "meta", label: "Muse" },
  { slug: "api", label: "Your own code" },
];

const Tile = ({ marks, title, line, className, children }: { marks: readonly Mark[]; title: string; line: string; className: string; children: ReactNode }) => (
  <section className={`flex min-w-0 flex-col p-6 sm:p-8 md:row-span-4 md:grid md:grid-rows-subgrid ${SOFT} ${className}`}>
    <div className="flex h-6 items-center gap-2.5">
      {marks.map((mark) => (
        <RunnerMark key={mark.slug} {...mark} />
      ))}
    </div>
    <h2 className="mt-6 text-title">{title}</h2>
    <p className="mt-1.5 text-pretty text-muted">{line}</p>
    <div className="mt-6 flex flex-wrap content-start items-center gap-x-4 gap-y-3">{children}</div>
  </section>
);

const KeySheet = ({ agent }: { agent: NewAgent }) => {
  const navigate = useNavigate();
  const rename = useRenameAgent(agent.eth_address);
  const listed = useAgents().data?.some((candidate) => candidate.eth_address === agent.eth_address);
  const [name, setName] = useState(agent.handle);
  const [saved, setSaved] = useState(agent.handle);
  const [invalid, setInvalid] = useState(false);
  const [copied, setCopied] = useState(false);
  const value = name.trim();
  const failed = rename.isError && rename.variables === value;
  const note = invalid
    ? "Use up to 15 letters, digits or underscores."
    : failed && (rename.error instanceof ApiError && rename.error.status === 409 ? "That name is taken." : "Could not rename the agent. Try again in a moment.");
  const open = () => navigate({ to: "/agents/$address", params: { address: agent.eth_address } });
  const commit = (then: () => void) => {
    if (value === saved) then();
    else if (!RULE.test(value)) setInvalid(true);
    else rename.mutate(value, { onSuccess: then });
  };
  const save = (event: FormEvent) => {
    event.preventDefault();
    commit(() => setSaved(value));
  };

  return (
    <Sheet onClose={open} locked>
      <form onSubmit={save} className="flex items-center gap-3">
        <Robot address={agent.eth_address} className="size-12" />
        <div className="relative min-w-0 flex-1">
          <Input
            aria-label="Agent name"
            aria-invalid={Boolean(note)}
            maxLength={15}
            spellCheck={false}
            autoComplete="off"
            value={name}
            onChange={(event) => {
              setName(event.target.value);
              setInvalid(false);
            }}
            className="pr-20 text-body font-medium"
          />
          {value === saved ? (
            <Pencil size={14} strokeWidth={1.75} className="pointer-events-none absolute top-1/2 right-4 -translate-y-1/2 text-faint" />
          ) : (
            <Button type="submit" disabled={rename.isPending} className="absolute top-[5px] right-[5px]">
              Save
            </Button>
          )}
        </div>
      </form>
      {note && (
        <p role="alert" className="mt-2 pl-19 text-caption">
          {note}
        </p>
      )}
      <Well text={agent.api_key} mono className="mt-6 bg-surface" onCopied={() => setCopied(true)} />
      <p className="mt-2 pl-4 text-caption text-muted">Shown only once. Paste it into your app now.</p>
      <div className="relative mt-6">
        <Button variant={copied ? "primary" : "secondary"} size="lg" disabled={rename.isPending || !listed} onClick={() => (note ? open() : commit(open))}>
          Done
        </Button>
        <details>
          <summary className="absolute top-0 right-0 flex h-10 cursor-pointer items-center text-muted underline decoration-line underline-offset-4 transition-colors duration-150 select-none hover:text-ink hover:decoration-ink [&::-webkit-details-marker]:hidden">
            Developer details
          </summary>
          <div className="mt-4 grid gap-2 text-caption text-muted">
            <p>
              Send the key as <span className="font-mono text-ink">X-API-Key</span> to the REST API, or as <span className="font-mono whitespace-nowrap text-ink">Authorization: Bearer</span> to this
              MCP server.
            </p>
            <div className="flex min-w-0 items-center gap-3">
              <span className="min-w-0 truncate font-mono text-ink">{MCP}</span>
              <Copy text={MCP} />
            </div>
          </div>
        </details>
      </div>
    </Sheet>
  );
};

export const AddAgent = () => {
  const navigate = useNavigate();
  const agents = useAgents().data;
  const create = useCreateAgent();
  const [known, setKnown] = useState<ReadonlySet<string>>();
  if (agents && !known) setKnown(new Set(agents.map((agent) => agent.eth_address)));
  if (!known) return null;
  const arrived = agents?.find((agent) => !known.has(agent.eth_address) && agent.runner.slug !== "api");

  return (
    <>
      {arrived ? (
        <>
          <Robot address={arrived.eth_address} className="size-16" />
          <h1 className="mt-5 text-title">{arrived.name} is connected</h1>
          <p className="mt-1 text-muted">Next, it asks in your chat what to call it and how to trade.</p>
          <Button variant="primary" size="lg" className="mt-6" onClick={() => navigate({ to: "/agents/$address", params: { address: arrived.eth_address } })}>
            Open {arrived.name}
          </Button>
        </>
      ) : (
        <>
          {known.size > 0 ? (
            <div className="flex min-w-0 items-baseline gap-2 text-title">
              <Link to="/" className="shrink-0 text-muted transition-colors duration-150 hover:text-ink">
                My agents
              </Link>
              <span className="text-faint" aria-hidden="true">
                /
              </span>
              <h1 className="min-w-0 truncate">Add an agent</h1>
            </div>
          ) : (
            <h1 className="text-title">Add your first agent</h1>
          )}
          <div className="mt-6 grid gap-3 sm:mt-8 md:grid-cols-12 md:gap-x-5 md:gap-y-0">
            <Tile marks={AGENTS} title="Set up with a prompt" line="Copy this text into Claude Code, Codex or another AI agent." className="md:col-span-7">
              <Well text={PROMPT} className="w-full bg-paper" />
            </Tile>
            <Tile marks={APPS} title="Use the API directly" line="Get an API key for Muse, a script or your own code." className="md:col-span-5">
              <Button variant="paper" size="lg" disabled={create.isPending} onClick={() => create.mutate()}>
                {create.isPending ? "Generating key" : create.isError ? "Try again" : "Generate API key"}
              </Button>
              {create.isError && <p role="alert">{(create.error instanceof ApiError && create.error.detail) || "AgentPit is busy, so nothing was created."}</p>}
            </Tile>
          </div>
        </>
      )}
      {create.data && <KeySheet agent={create.data} />}
    </>
  );
};
