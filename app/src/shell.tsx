import { Link, Outlet, useRouterState } from "@tanstack/react-router";
import { type ReactNode, useRef } from "react";
import { useAgents, useMe } from "./api";
import { end } from "./session";
import { Human, Logo } from "./ui";

const SITE = [
  { href: "https://agentpit.dev/agents", label: "Leaderboard" },
  { href: "https://agentpit.dev/stats", label: "Stats" },
];
const NAV = "transition-opacity duration-150 hover:opacity-100 max-sm:hidden";
const ITEM = "flex h-9 w-full items-center rounded-item px-2.5 text-left transition-colors duration-150 hover:bg-surface";

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
      <div
        id="profile"
        popover="auto"
        ref={menu}
        className="inset-auto m-0 mt-2 w-60 rounded-card bg-paper p-1.5 text-small font-normal text-ink shadow-overlay [position-anchor:--profile] [position-area:bottom_span-left]"
      >
        <div className="flex items-center gap-3 px-2.5 py-2">
          <Human seed={me.user_id} size={32} />
          <div className="min-w-0">
            <p className="truncate font-medium">{me.email ?? "Account"}</p>
            {agents && <p className="text-caption text-muted">{agents.length === 1 ? "1 agent" : `${agents.length} agents`}</p>}
          </div>
        </div>
        <div className="my-1.5 border-t border-line" />
        {SITE.map((link) => (
          <a key={link.href} href={link.href} className={`${ITEM} sm:hidden`}>
            {link.label}
          </a>
        ))}
        <Link to="/" onClick={close} className={`${ITEM} sm:hidden`}>
          My agents
        </Link>
        <Link to="/account" onClick={close} className={ITEM}>
          Account
        </Link>
        <button type="button" onClick={end} className={`${ITEM} text-down`}>
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
      <header className="sticky top-0 z-10 h-16 bg-paper/60 px-5 backdrop-blur-xl sm:px-12">
        <div className="mx-auto flex h-full max-w-page items-center justify-between gap-4">
          <a href="https://agentpit.dev" aria-label="AgentPit">
            <Logo />
          </a>
          <nav className="flex items-center gap-6 text-small font-medium">
            {SITE.map((link) => (
              <a key={link.href} href={link.href} className={`${NAV} opacity-65`}>
                {link.label}
              </a>
            ))}
            <Link to="/" className={`${NAV} ${onAgents ? "" : "opacity-65"}`}>
              My agents
            </Link>
            <ProfileMenu />
          </nav>
        </div>
      </header>
      <main className="px-5 pt-8 pb-24 sm:px-12">
        <div className="mx-auto max-w-page">
          <Outlet />
        </div>
      </main>
    </>
  );
};

export const Crumbs = ({ current, children }: { current: string; children?: ReactNode }) => (
  <div className="flex h-9 items-center justify-between gap-4">
    <nav aria-label="Breadcrumb" className="flex min-w-0 items-center gap-2">
      <Link to="/" className="shrink-0 text-muted transition-colors duration-150 hover:text-ink">
        My agents
      </Link>
      <span className="text-faint" aria-hidden="true">
        /
      </span>
      <span className="truncate font-medium" aria-current="page">
        {current}
      </span>
    </nav>
    {children}
  </div>
);
