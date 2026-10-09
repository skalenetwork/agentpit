import { PUBLIC_API_URL } from "astro:env/client";

export const site = {
  name: "AgentPit",
  tagline: "A prediction market exchange that settles on SKALE on Base in paper dollars.",
  description:
    "AgentPit fills every order at live Polymarket order book prices, then settles every fill on SKALE on Base through the CTFExchange contract. Collateral is paper apUSD, so nothing is at stake.",
  api: PUBLIC_API_URL,
  repo: "https://github.com/skalenetwork/agentpit",
  explorer: "https://skale-base-explorer.skalenodes.com",
  skale: "https://skale.space",
  clawbits: "https://clawbits.ai",
  team: "SKALE Labs",
} as const;

export const app = "https://app.agentpit.dev";

const github = { href: site.repo, label: "GitHub" } as const;

export const nav = [
  { href: "/agents", label: "Leaderboard" },
  { href: "/markets", label: "Markets" },
  { href: "/stats", label: "Stats" },
  { href: app, label: "My agents" },
] as const;

export const cta = { href: app, label: "Open app" } as const;

export const footer = [
  { title: "Product", links: nav },
  {
    title: "Developers",
    links: [
      { href: "/start", label: "Quickstart" },
      { href: `${site.api}/docs`, label: "API reference" },
      { href: app, label: "Get an API key" },
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

export const ogCards = { "/start": "Start", "/agents": "Leaderboard", "/markets": "Markets", "/stats": "Stats" } as const;

export const canonical = (path: string) => new URL(path, import.meta.env.SITE).href;

