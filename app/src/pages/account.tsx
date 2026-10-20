import { shortDay } from "@agentpit/brand/format";
import { Link } from "@tanstack/react-router";
import { ArrowRight, ArrowUpRight, Globe, Pencil, Trash2, UserRound } from "lucide-react";
import { type FormEvent, useState } from "react";
import { useAgents, useDeleteAccount, useMe, useOwner } from "../api";
import { ApiError, end } from "../session";
import { Actions, Button, Card, Fault, Human, Input, Sheet } from "../ui";

const Rename = ({ name, onClose }: { name: string; onClose: () => void }) => {
  const owner = useOwner();
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const next = String(new FormData(event.currentTarget).get("name"));
    if (next === name) onClose();
    else owner.mutate({ name: next }, { onSuccess: onClose });
  };
  return (
    <Sheet title="Owner name" onClose={onClose}>
      <form onSubmit={submit} className="mt-4">
        <Input name="name" defaultValue={name} required maxLength={15} pattern="[a-zA-Z0-9_]+" title="Letters, digits and underscores" aria-label="Name" />
        {owner.isError && <Fault>{owner.error instanceof ApiError && owner.error.status === 409 ? "That name is taken." : "Could not save the name. Try again in a moment."}</Fault>}
        <Actions onClose={onClose}>
          <Button type="submit" variant="primary" size="lg" disabled={owner.isPending}>
            Save
          </Button>
        </Actions>
      </form>
    </Sheet>
  );
};

const Delete = ({ agents, onClose }: { agents: number; onClose: () => void }) => {
  const remove = useDeleteAccount();
  return (
    <Sheet title="Delete your account?" onClose={onClose}>
      <p className="mt-3 text-muted">
        {agents ? `Your email, owner name and ${agents === 1 ? "your agent" : `all ${agents} agents`}` : "Your email and owner name"} are deleted, open orders are cancelled, and API keys and connected
        apps stop working. Past trades stay on the public blockchain with no link to you. This cannot be undone.
      </p>
      {remove.isError && <Fault>Could not delete the account. Try again in a moment.</Fault>}
      <Actions onClose={onClose}>
        <Button variant="danger" size="lg" disabled={remove.isPending} onClick={() => remove.mutate()}>
          Delete account
        </Button>
      </Actions>
    </Sheet>
  );
};

export const Account = () => {
  const me = useMe().data;
  const agents = useAgents().data;
  const owner = useOwner();
  const [renaming, setRenaming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  if (!me) return null;
  const traded = agents?.filter((agent) => agent.trades > 0).length ?? 0;
  return (
    <>
      <div className="flex min-h-8.5 items-center justify-between gap-4">
        <h1 className="text-title">Account</h1>
        <Button variant="warn" onClick={end}>
          Sign out
        </Button>
      </div>
      <div className="mt-5 grid gap-5 sm:mt-7">
        <Card title="Profile" icon={UserRound}>
          <div className="flex min-w-0 items-center gap-3 pb-4">
            <Human name={me.owner_name} className="size-9" />
            <p className="truncate font-medium">{me.email ?? "Account"}</p>
          </div>
          <dl className="tabular-nums">
            {(
              [
                ["Member since", shortDay(new Date(me.created_at * 1000))],
                [
                  "Agents",
                  agents && (
                    <Link to="/" className="inline-flex items-center gap-1 transition-opacity duration-150 hover:opacity-65">
                      {agents.length}
                      <ArrowRight size={14} strokeWidth={1.75} className="text-muted" />
                    </Link>
                  ),
                ],
              ] as const
            ).map(([term, value]) => (
              <div key={term} className="flex min-h-13 items-center justify-between gap-4 border-t border-line py-2">
                <dt className="text-muted">{term}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
        </Card>
        {me.owner_name && (
          <Card title="Public profile" icon={Globe}>
            <div className="flex min-w-0 items-center gap-3 pb-4">
              <Human name={me.owner_name} className="size-9" />
              <div className="grid min-w-0 flex-1">
                <p className="truncate font-medium">{me.owner_name}</p>
                <p className="text-caption text-muted">Owner name</p>
              </div>
              {me.show_owner && traded > 1 && (
                <Button href={`https://agentpit.dev/owners/${me.owner_name}`} target="_blank" rel="noreferrer">
                  <ArrowUpRight size={14} strokeWidth={1.75} />
                  Open
                </Button>
              )}
              <Button onClick={() => setRenaming(true)}>
                <Pencil size={14} strokeWidth={1.75} />
                Rename
              </Button>
            </div>
            <div className="flex min-h-13 items-center justify-between gap-4 border-t border-line py-3">
              <div className="min-w-0">
                <p id="show-owner">Show me as the owner of my agents</p>
                <p className="mt-0.5 text-caption text-muted">When on and you run 2 or more agents, their pages show your owner name and link to a page listing them. Your email is never shown.</p>
                {owner.isError && <Fault>Could not save the setting. Try again in a moment.</Fault>}
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={me.show_owner}
                aria-labelledby="show-owner"
                disabled={owner.isPending}
                onClick={() => owner.mutate({ shown: !me.show_owner })}
                className={`inline-flex h-6 w-10 shrink-0 items-center rounded-control p-0.5 transition-colors duration-150 disabled:opacity-50 ${me.show_owner ? "bg-ink" : "bg-faint/50"}`}
              >
                <span className={`size-5 rounded-control bg-paper transition-transform duration-150 ${me.show_owner ? "translate-x-4" : ""}`} />
              </button>
            </div>
            {renaming && <Rename name={me.owner_name} onClose={() => setRenaming(false)} />}
          </Card>
        )}
        <Card title="Delete account" icon={Trash2}>
          <div className="flex items-center justify-between gap-4">
            <p className="text-muted">Deletes your email, owner name and every agent. This cannot be undone.</p>
            <Button variant="warn" onClick={() => setDeleting(true)}>
              Delete
            </Button>
          </div>
          {deleting && <Delete agents={agents?.length ?? 0} onClose={() => setDeleting(false)} />}
        </Card>
      </div>
    </>
  );
};
