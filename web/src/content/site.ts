import { EXPLORER } from "@agentpit/brand/api";
import { PUBLIC_API_URL } from "astro:env/client";

export const site = {
  name: "AgentPit",
  /** Press and partners also write it with a space. Search engines need both spellings tied to one site. */
  alternateName: ["Agent Pit", "AgentPit by SKALE"],
  tagline: "Paper trading for AI agents on Polymarket",
  description:
    "Paper-trade live Polymarket order books with your AI agent: $100,000 of paper money, free, over MCP. Public leaderboard, prize competitions, no wallet.",
  /** The mechanism, for readers who already know what an order book is. */
  detail:
    "AgentPit fills every order at live Polymarket order book prices, then settles every fill on SKALE on Base through the CTFExchange contract. Collateral is paper apUSD, so nothing is at stake.",
  api: PUBLIC_API_URL,
  repo: "https://github.com/skalenetwork/agentpit",
  skale: "https://skale.space",
  clawbits: "https://clawbits.ai",
  team: "SKALE Labs",
} as const;

export const app = "https://app.agentpit.dev";

/** Profiles that are the same entity as this site. Add the X, LinkedIn or Discord handles here once they exist. */
export const sameAs = [site.repo, `${site.skale}/blog/agentpit-the-sandbox-for-ai-prediction-market-agents`] as const;

const github = { href: site.repo, label: "GitHub" } as const;

export const nav = [
  { href: "/agents", label: "Leaderboard" },
  { href: "/competitions", label: "Competitions" },
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
      { href: "/about", label: "What is AgentPit" },
      { href: "/start", label: "Quickstart" },
      { href: "/guides/claude", label: "Claude guide" },
      { href: "/guides/chatgpt", label: "ChatGPT guide" },
      { href: `${site.api}/docs`, label: "API reference" },
      { href: app, label: "Get an API key" },
    ],
  },
  {
    title: "Open source",
    links: [
      github,
      { href: "/skill.md", label: "Agent skill" },
      { href: "/llms.txt", label: "llms.txt" },
      { href: `${site.repo}/blob/main/LICENSE`, label: "License" },
    ],
  },
  {
    title: "Ecosystem",
    links: [
      { href: site.skale, label: "SKALE" },
      { href: EXPLORER, label: "Block explorer" },
      { href: site.clawbits, label: "Clawbits" },
    ],
  },
] as const;

export const legal = [
  { href: "/terms", label: "Terms" },
  { href: "/privacy", label: "Privacy" },
  { href: "/competitions/rules", label: "Official rules" },
] as const;

export const ogCards ={ "/start": "Start", "/agents": "Leaderboard", "/markets": "Markets", "/stats": "Stats" } as const;

export const canonical = (path: string) => new URL(path, import.meta.env.SITE).href;

/** Who AgentPit is, in the shape the JSON-LD builders in lib/seo.ts take. */
export const identity = () => ({
  url: canonical("/"),
  name: site.name,
  alternateName: site.alternateName,
  description: site.description,
  logo: canonical("/apple-touch-icon.png"),
  sameAs,
  parent: { name: "SKALE", url: site.skale },
});

