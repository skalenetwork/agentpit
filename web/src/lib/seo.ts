import type { RaceHead } from "@agentpit/brand/api";

/** Schema.org JSON-LD builders. Every node carries an @id so pages can link nodes instead of repeating them. */

export type Node = Record<string, unknown>;

interface Site {
  readonly url: string;
  readonly name: string;
  readonly alternateName: readonly string[];
  readonly description: string;
  readonly logo: string;
  readonly sameAs: readonly string[];
  readonly parent: { readonly name: string; readonly url: string };
}

const id = (site: Site, fragment: string) => `${site.url}#${fragment}`;

export const organization = (site: Site): Node => ({
  "@type": "Organization",
  "@id": id(site, "organization"),
  name: site.name,
  alternateName: site.alternateName,
  url: site.url,
  logo: { "@type": "ImageObject", url: site.logo },
  description: site.description,
  sameAs: site.sameAs,
  parentOrganization: { "@type": "Organization", name: site.parent.name, url: site.parent.url },
});

export const website = (site: Site): Node => ({
  "@type": "WebSite",
  "@id": id(site, "website"),
  name: site.name,
  alternateName: site.alternateName,
  url: site.url,
  inLanguage: "en",
  publisher: { "@id": id(site, "organization") },
});

export const application = (site: Site, features: readonly string[]): Node => ({
  "@type": "WebApplication",
  "@id": id(site, "application"),
  name: site.name,
  alternateName: site.alternateName,
  url: site.url,
  description: site.description,
  applicationCategory: "FinanceApplication",
  applicationSubCategory: "Paper trading for AI agents",
  operatingSystem: "Any",
  isAccessibleForFree: true,
  offers: { "@type": "Offer", price: "0", priceCurrency: "USD" },
  featureList: features,
  publisher: { "@id": id(site, "organization") },
});

export const faq = (facts: readonly { readonly term: string; readonly def: string }[]): Node => ({
  "@type": "FAQPage",
  mainEntity: facts.map(({ term, def }) => ({ "@type": "Question", name: term, acceptedAnswer: { "@type": "Answer", text: def } })),
});

export const breadcrumbs = (trail: readonly { readonly name: string; readonly url: string }[]): Node => ({
  "@type": "BreadcrumbList",
  itemListElement: trail.map(({ name, url }, i) => ({ "@type": "ListItem", position: i + 1, name, item: url })),
});

/** The `**Question?** Answer` paragraphs of a Markdown page, as FAQ pairs. Links are reduced to their text. */
export const questions = (markdown: string): { term: string; def: string }[] =>
  [...markdown.matchAll(/^\*\*(.+?\?)\*\* (.+)$/gm)].map(([, term, def]) => ({ term, def: def.replace(/\[([^\]]+)\]\([^)]+\)/g, "$1") }));

/** A written page with its date, so answer engines can see how fresh it is. */
export const article = (site: Site, url: string, headline: string, description: string, modified: string): Node => ({
  "@type": "TechArticle",
  "@id": `${url}#article`,
  headline,
  description,
  url,
  inLanguage: "en",
  dateModified: modified,
  author: { "@id": id(site, "organization") },
  publisher: { "@id": id(site, "organization") },
  isPartOf: { "@id": id(site, "website") },
});

const iso = (seconds: number) => new Date(seconds * 1000).toISOString();

const status = (phase: RaceHead["phase"]) => (phase === "ended" ? "EventCompleted" : "EventScheduled");

/** An online competition. Prize money becomes offers, so search engines can show the pool. */
export const competition = (site: Site, race: RaceHead, url: string, image: string): Node => {
  const pool = race.prizes.reduce((sum, prize) => sum + prize.usd, 0);
  return {
    "@type": "Event",
    "@id": `${url}#event`,
    name: race.title,
    description: pool > 0 ? `${race.line} $${pool.toLocaleString("en-US")} in prizes.` : race.line,
    url,
    image,
    startDate: iso(race.startsAt),
    endDate: iso(race.endsAt),
    eventStatus: `https://schema.org/${status(race.phase)}`,
    eventAttendanceMode: "https://schema.org/OnlineEventAttendanceMode",
    location: { "@type": "VirtualLocation", url },
    organizer: { "@type": "Organization", "@id": id(site, "organization"), name: site.name, url: site.url },
    isAccessibleForFree: true,
    offers: {
      "@type": "Offer",
      price: "0",
      priceCurrency: "USD",
      url,
      validFrom: iso(race.startsAt - 30 * 86400),
      availability: `https://schema.org/${race.phase === "upcoming" || race.phase === "live" ? "InStock" : "SoldOut"}`,
    },
  };
};

/** One script body. `<` is escaped so a market title can never close the script tag. */
export const graph = (nodes: readonly Node[]): string =>
  JSON.stringify({ "@context": "https://schema.org", "@graph": nodes }).replace(/</g, "\\u003c");

/** Search and answer engines that may crawl and cite every public page. */
export const crawlers = [
  "Googlebot",
  "Bingbot",
  "OAI-SearchBot",
  "ChatGPT-User",
  "GPTBot",
  "Claude-SearchBot",
  "Claude-User",
  "ClaudeBot",
  "PerplexityBot",
  "Perplexity-User",
  "Google-Extended",
  "Applebot",
  "Applebot-Extended",
  "DuckAssistBot",
  "MistralAI-User",
] as const;

export const robots = (sitemap: string): string =>
  [
    "# AgentPit is public. Search engines and AI assistants are welcome to crawl, cite and learn from it.",
    "# Content-Signal: search=yes, ai-input=yes, ai-train=yes",
    "",
    ...crawlers.map((agent) => `User-agent: ${agent}`),
    "Allow: /",
    "",
    "User-agent: *",
    "Content-Signal: search=yes, ai-input=yes, ai-train=yes",
    "Allow: /",
    "",
    `Sitemap: ${sitemap}`,
    "",
  ].join("\n");

export interface Entry {
  readonly loc: string;
  readonly lastmod?: string;
  readonly changefreq?: "hourly" | "daily" | "weekly" | "monthly" | "yearly";
  readonly priority?: number;
}

const escape = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

export const sitemap = (entries: readonly Entry[]): string =>
  `<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">${entries
    .map(
      ({ loc, lastmod, changefreq, priority }) =>
        `<url><loc>${escape(loc)}</loc>${lastmod ? `<lastmod>${lastmod}</lastmod>` : ""}${changefreq ? `<changefreq>${changefreq}</changefreq>` : ""}${priority === undefined ? "" : `<priority>${priority.toFixed(1)}</priority>`}</url>`,
    )
    .join("")}</urlset>`;
