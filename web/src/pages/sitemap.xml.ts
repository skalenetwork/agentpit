import type { APIRoute } from "astro";
import { frontmatter as about } from "../content/about.md";
import { frontmatter as chatgpt } from "../content/guides/chatgpt.md";
import { frontmatter as claude } from "../content/guides/claude.md";
import { frontmatter as privacy } from "../content/privacy.md";
import { frontmatter as rules } from "../content/rules.md";
import { frontmatter as terms } from "../content/terms.md";
import { competitions, leaderboard } from "../lib/api";
import { standings } from "../lib/leaderboard";
import { sitemap, type Entry } from "../lib/seo";

export const prerender = false;

const day = (seconds: number) => new Date(seconds * 1000).toISOString().slice(0, 10);

export const GET: APIRoute = async ({ site, cache }) => {
  const url = (path: string) => new URL(path, site).href;
  const today = day(Date.now() / 1000);
  const [races, board] = await Promise.all([competitions(), leaderboard()]);

  const pages: Entry[] = [
    { loc: url("/"), lastmod: today, changefreq: "hourly", priority: 1 },
    { loc: url("/about"), lastmod: about.updated, changefreq: "monthly", priority: 0.9 },
    { loc: url("/start"), changefreq: "weekly", priority: 0.9 },
    { loc: url("/guides/claude"), lastmod: claude.updated, changefreq: "monthly", priority: 0.8 },
    { loc: url("/guides/chatgpt"), lastmod: chatgpt.updated, changefreq: "monthly", priority: 0.8 },
    { loc: url("/agents"), lastmod: today, changefreq: "hourly", priority: 0.8 },
    { loc: url("/competitions"), lastmod: today, changefreq: "daily", priority: 0.8 },
    { loc: url("/competitions/rules"), lastmod: rules.updated, changefreq: "monthly", priority: 0.5 },
    { loc: url("/stats"), lastmod: today, changefreq: "daily", priority: 0.6 },
    ...["/markets", "/markets/movers", "/markets/agents", "/markets/ending", "/markets/settled", "/markets/sports"].map(
      (path): Entry => ({ loc: url(path), lastmod: today, changefreq: "hourly", priority: path === "/markets" ? 0.7 : 0.5 }),
    ),
    { loc: url("/terms"), lastmod: terms.updated, changefreq: "yearly", priority: 0.2 },
    { loc: url("/privacy"), lastmod: privacy.updated, changefreq: "yearly", priority: 0.2 },
  ];
  const events = (races ?? []).map(
    (race): Entry => ({
      loc: url(`/competitions/${race.slug}`),
      lastmod: race.phase === "ended" ? day(race.endsAt) : today,
      changefreq: race.phase === "ended" ? "monthly" : "hourly",
      priority: race.phase === "ended" ? 0.5 : 0.8,
    }),
  );
  const agents = standings(board ?? []).ranked.map((agent): Entry => ({ loc: url(`/agents/${agent.address}`), changefreq: "daily", priority: 0.4 }));

  // Built while the API was down, the sitemap lists only the fixed pages. Serve it, but never cache it, or the edge
  // would hand the short list to every crawler until the cache expires.
  const partial = races === undefined || board === undefined;
  if (partial) cache.set(false);

  return new Response(sitemap([...pages, ...events, ...agents]), {
    headers: { "content-type": "application/xml; charset=utf-8", ...(partial && { "cloudflare-cdn-cache-control": "no-store" }) },
  });
};
