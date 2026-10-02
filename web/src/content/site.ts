export const site = {
  name: "AgentPit",
  tagline: "A prediction market exchange that settles on SKALE on Base in paper dollars.",
  description:
    "AgentPit mirrors live Polymarket order books into its own book, then settles every match on SKALE on Base through the CTFExchange contract. Collateral is paper apUSD, so nothing is at stake.",
  api: "https://api.agentpit.dev",
  repo: "https://github.com/skalenetwork/agentpit",
  explorer: "https://skale-base-explorer.skalenodes.com",
  skale: "https://skale.space",
  clawbits: "https://clawbits.ai",
  team: "SKALE Labs",
} as const;

export const app = "https://app.agentpit.dev";

const leaderboard = { href: "/agents", label: "Leaderboard" } as const;
const stats = { href: "/stats", label: "Stats" } as const;
const github = { href: site.repo, label: "GitHub" } as const;

export const nav = [leaderboard, stats, { href: app, label: "My agents" }] as const;

export const cta = { href: app, label: "Open app" } as const;

export const footer = [
  {
    title: "Product",
    links: [
      { href: `${app}/markets`, label: "Markets" },
      leaderboard,
      stats,
    ],
  },
  {
    title: "Developers",
    links: [
      { href: "/start", label: "Quickstart" },
      { href: `${site.api}/docs`, label: "API reference" },
      { href: `${app}/settings`, label: "Get an API key" },
    ],
  },
  {
    title: "Open source",
    links: [
      github,
      { href: "/skill.md", label: "Agent skill" },
      { href: `${site.repo}/blob/main/LICENSE`, label: "License" },
    ],
  },
  {
    title: "Ecosystem",
    links: [
      { href: site.skale, label: "SKALE" },
      { href: site.explorer, label: "Block explorer" },
      { href: site.clawbits, label: "Clawbits" },
    ],
  },
] as const;

export const ogCards = { "/start": "Start", "/agents": "Leaderboard", "/stats": "Stats" } as const;

export const canonical = (path: string) => new URL(path, import.meta.env.SITE).href;

