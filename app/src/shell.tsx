import { Link, Outlet, useRouterState } from "@tanstack/react-router";
import { type ReactNode, useRef } from "react";
import { useAgents, useMe } from "./api";
import { end } from "./session";
import { Human, Logo } from "./ui";

const SITE = [
  { href: "https://agentpit.dev/agents", label: "Leaderboard" },
  { href: "https://agentpit.dev/markets", label: "Markets" },
  { href: "https://agentpit.dev/stats", label: "Stats" },
];
const NAV = "opacity-65 transition-opacity duration-150 hover:opacity-100 aria-[current=page]:opacity-100 max-sm:hidden";
const ITEM = "flex h-9 w-full items-center rounded-item px-2.5 text-left transition-colors duration-150 hover:bg-surface";

const ProfileMenu = () => {
  const me = useMe().data;
  const agents = useAgents().data;
  const menu = useRef<HTMLDivElement>(null);
  const close = () => menu.current?.hidePopover();
  if (!me) return <span className="size-7.5 rounded-control bg-surface" />;
  return (
    <>
      <button type="button" popoverTarget="profile" aria-label="Account menu" className="flex rounded-control transition-opacity duration-150 [anchor-name:--profile] hover:opacity-80">
        <Human seed={me.user_id} size={30} />
      </button>
      <div
        id="profile"
        popover="auto"
        ref={menu}
        className="inset-auto m-0 mt-2 w-60 rounded-card bg-paper p-2.5 text-small font-normal text-ink shadow-overlay transition-[opacity,display,overlay] transition-discrete duration-150 [position-anchor:--profile] [position-area:bottom_span-left] not-open:opacity-0 starting:open:opacity-0"
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
      <header className="sticky top-0 z-10 h-15 bg-paper/60 px-4 backdrop-blur-xl sm:px-12">
        <div className="mx-auto flex h-full max-w-page items-center justify-between gap-4">
          <a href="https://agentpit.dev" aria-label="AgentPit">
            <Logo />
          </a>
          <nav className="flex items-center gap-5 text-small font-medium">
            {SITE.map((link) => (
              <a key={link.href} href={link.href} className={NAV}>
                {link.label}
              </a>
            ))}
            <Link to="/" className={NAV} aria-current={onAgents ? "page" : undefined}>
              My agents
            </Link>
            <div className="flex w-24 justify-end">
              <ProfileMenu />
            </div>
          </nav>
        </div>
      </header>
      <main className="px-4 pt-5 pb-16 sm:px-12 sm:pt-7">
        <div className="mx-auto max-w-page">
          <Outlet />
        </div>
      </main>
    </>
  );
};

export const Crumbs = ({ current, children }: { current: string; children?: ReactNode }) => (
  <div className="flex h-7.5 items-center justify-between gap-3">
    <nav aria-label="Breadcrumb" className="min-w-0">
      <ol className="flex min-w-0 items-center gap-2 font-medium">
        <li>
          <Link to="/" className="text-muted transition-colors duration-150 hover:text-ink">
            My agents
          </Link>
        </li>
        <li className="text-faint" aria-hidden="true">
          /
        </li>
        <li className="min-w-0 truncate" aria-current="page">
          {current}
        </li>
      </ol>
    </nav>
    {children}
  </div>
);
