import { shortDay } from "@agentpit/brand/format";
import { useAgents, useMe } from "../api";
import { end } from "../session";
import { Crumbs } from "../shell";
import { Button, Fact, Human } from "../ui";

export const Account = () => {
  const me = useMe().data;
  const agents = useAgents().data;
  if (!me) return null;
  return (
    <>
      <Crumbs current="Account">
        <Button variant="ghost" className="-mr-3.5" onClick={end}>
          Sign out
        </Button>
      </Crumbs>
      <div className="mt-6 flex items-center gap-4">
        <Human seed={me.user_id} size={56} />
        <h1 className="truncate text-title">{me.email ?? "Account"}</h1>
      </div>
      <dl className="mt-10 max-w-text divide-y divide-line border-y border-line">
        <Fact label="Member since">{shortDay(new Date(me.created_at * 1000))}</Fact>
        <Fact label="Agents">{agents?.length}</Fact>
      </dl>
    </>
  );
};
