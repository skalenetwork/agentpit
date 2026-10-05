import { shortDay } from "@agentpit/brand/format";
import { Link } from "@tanstack/react-router";
import { ArrowRight, UserRound } from "lucide-react";
import { useAgents, useMe } from "../api";
import { end } from "../session";
import { Button, Card, Human } from "../ui";

export const Account = () => {
  const me = useMe().data;
  const agents = useAgents().data;
  if (!me) return null;
  return (
    <>
      <div className="flex min-h-8.5 items-center justify-between gap-4">
        <h1 className="text-title">Account</h1>
        <Button variant="warn" onClick={end}>
          Sign out
        </Button>
      </div>
      <div className="mt-5 sm:mt-7">
        <Card title="Profile" icon={UserRound}>
          <div className="flex min-w-0 items-center gap-3 pb-4">
            <Human seed={me.user_id} size={36} />
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
      </div>
    </>
  );
};
