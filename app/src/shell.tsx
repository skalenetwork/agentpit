import { Link, Outlet, useRouterState } from "@tanstack/react-router";
import { ArrowUpRight } from "lucide-react";
import { type ReactNode, useRef } from "react";
import { useAgents, useMe } from "./api";
import { end } from "./session";
import { Human, Logo } from "./ui";

const LEADERBOARD = "https://agentpit.dev/agents";
const NAV = "text-small font-medium transition-colors duration-150 hover:text-ink";
const ITEM = "flex h-9 w-full items-center justify-between gap-3 rounded-item px-2.5 text-left text-small text-ink transition-colors duration-150 hover:bg-surface";

const Out = () => <ArrowUpRight size={14} strokeWidth={1.75} className="text-faint" />;

const ProfileMenu = () => {
  const me = useMe().data;
  const agents = useAgents().data;
  const menu = useRef<HTMLDivElement>(null);
  const close = () => menu.current?.hidePopover();
  if (!me) return <span className="size-8 rounded-control bg-surface" />;
  return (
    <>
      <button type="button" popoverTarget="profile" aria-label="Account menu" className="flex rounded-control transition-opacity duration-150 [anchor-name:--profile] hover:opacity-80">
        <Human seed={me.user_id} size={32} />
      </button>
      <div id="profile" popover="auto" ref={menu} className="inset-auto m-0 mt-2 w-60 rounded-card bg-paper p-1.5 text-ink shadow-overlay [position-anchor:--profile] [position-area:bottom_span-left]">
        <div className="flex items-center gap-3 px-2.5 py-2">
          <Human seed={me.user_id} size={32} />
          <div className="min-w-0">
            <p className="truncate text-small font-medium">{me.email ?? "Account"}</p>
            {agents && <p className="text-caption text-muted">{agents.length === 1 ? "1 agent" : `${agents.length} agents`}</p>}
          </div>
        </div>
        <div className="my-1.5 border-t border-line" />
        <Link to="/" onClick={close} className={`${ITEM} sm:hidden`}>
          Agents
        </Link>
        <a href={LEADERBOARD} target="_blank" rel="noreferrer" onClick={close} className={`${ITEM} sm:hidden`}>
          Leaderboard
          <Out />
        </a>
        <Link to="/account" onClick={close} className={ITEM}>
          Account
        </Link>
        <button type="button" onClick={end} className={ITEM}>
          Sign out
        </button>
      </div>
    </>
  );
};

export const Shell = () => {
  const onAgents = useRouterState({ select: (state) => state.location.pathname === "/" || state.location.pathname.startsWith("/agents/") });
  return (
    <>
      <header className="sticky top-0 z-10 h-14 border-b border-line bg-paper px-5 sm:px-12">
        <div className="mx-auto flex h-full max-w-page items-center justify-between gap-4">
          <Link to="/" aria-label="AgentPit">
            <Logo />
          </Link>
          <div className="flex items-center gap-2">
            <nav className="mr-4 hidden items-center gap-6 sm:flex">
              <Link to="/" className={`${NAV} ${onAgents ? "text-ink" : "text-muted"}`}>
                Agents
              </Link>
              <a href={LEADERBOARD} target="_blank" rel="noreferrer" className={`${NAV} inline-flex items-center gap-1 text-muted`}>
                Leaderboard
                <Out />
              </a>
            </nav>
            <ProfileMenu />
          </div>
        </div>
      </header>
      <main className="px-5 pt-10 pb-24 sm:px-12">
        <div className="mx-auto max-w-page">
          <Outlet />
        </div>
      </main>
    </>
  );
};

export const Crumbs = ({ current, children }: { current: string; children?: ReactNode }) => (
  <div className="flex h-9 items-center justify-between gap-4">
    <nav aria-label="Breadcrumb" className="flex min-w-0 items-center gap-2 text-small">
      <Link to="/" className="text-muted transition-colors duration-150 hover:text-ink">
        Agents
      </Link>
      <span className="text-faint" aria-hidden="true">
        /
      </span>
      <span className="truncate font-medium text-ink" aria-current="page">
        {current}
      </span>
    </nav>
    {children}
  </div>
);
