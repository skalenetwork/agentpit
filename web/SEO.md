# Discoverability checklist

What is in the code, and what has to be done by hand in Cloudflare, Google, Bing and elsewhere.
Written 12 November 2026. At that date no search index we could query listed a single agentpit.dev page,
while press coverage (The Block, SKALE blog, bitbank) did rank, mostly as "Agent Pit".

## In the code

| What | Where |
|---|---|
| Titles that use the words people search ("paper trading", "AI agents", "Polymarket") | `src/content/site.ts`, agent and competition pages |
| `Organization` and `WebSite` JSON-LD on every page, with "Agent Pit" as an alternate name | `src/components/Layout.astro`, `src/lib/seo.ts` |
| `WebApplication` and `FAQPage` on the home page | `src/pages/index.astro` |
| `Event` with dates and prize pool on each competition, `ProfilePage` on each ranked agent, breadcrumbs | `src/pages/competitions/[slug].astro`, `src/pages/agents/[address].astro` |
| `/about`: an answer-first "What is AgentPit" page with a comparison table and FAQ, for answer engines to quote | `src/content/about.md` |
| `/llms.txt` and `/llms-full.txt` | `src/content/llms.ts`, `src/pages/llms*.txt.ts` |
| `robots.txt` names every search and AI crawler, and carries Content Signals | `src/lib/seo.ts` |
| Sitemap lists live competitions and ranked agents, with `lastmod` | `src/pages/sitemap.xml.ts` |
| Twitter card tags, `og:locale`, `max-image-preview:large` | `Layout.astro` |
| CSS served as cached files instead of inlined: static pages went from about 132 KB to about 30 KB of HTML | `astro.config.ts` |

## To do by hand

### Cloudflare (agentpit.dev zone)

1. **Security > Bots.** Turn **Bot Fight Mode** off, or confirm that **Verified bots** are allowed. Bot Fight Mode can challenge
   Googlebot, Bingbot and AI crawlers, and the challenge page then replaces the content they index.
2. **AI Crawl Control** (it used to be AI Audit). Set every search and assistant crawler to **Allow**: GPTBot, OAI-SearchBot,
   ChatGPT-User, ClaudeBot, Claude-SearchBot, PerplexityBot, Google-Extended and Applebot-Extended.
   Since July 2025 new Cloudflare zones block AI crawlers by default.
3. **Turn off the managed robots.txt** (under AI Crawl Control or Security > Settings). Otherwise Cloudflare puts its own
   rules in front of `robots.txt` from the code.
4. **Pay per crawl:** leave it off.
5. **Crawler Hints:** on (Caching > Configuration). It sends IndexNow pings to Bing and Yandex when pages change.
6. **Security > WAF:** check that no custom rule or rate limit challenges `/sitemap.xml`, `/robots.txt`, `/llms.txt` or `/skill.md`.
7. **Security level / Under Attack mode:** off for the apex.
8. **SSL/TLS > Edge Certificates:** turn on **Always Use HTTPS**. Today `http://agentpit.dev/` answers 200 instead of
   redirecting, so Google can see two copies of every page. Then turn on **HSTS** (max-age 6 months, include subdomains)
   once you are sure every subdomain serves HTTPS.
9. **Check from outside:** Security > Events, filtered by user agent `Googlebot` or by verified bot category. There should be no
   challenges or blocks.
10. **DNS:** add the TXT records Google Search Console and Bing Webmaster Tools give you (see below).

### Google

1. ~~**Search Console:** add a **Domain property** for `agentpit.dev`, verified by DNS TXT.~~ Done 13 November 2026.
2. Submit `https://agentpit.dev/sitemap.xml`.
3. **URL Inspection > Request indexing** for `/`, `/about`, `/start`, `/competitions`, `/agents` and the live competition page.
   Run **Test live URL** first. If the screenshot shows a Cloudflare challenge, go back to the Cloudflare steps.
4. After a week, check **Pages > Why pages aren't indexed** and **Enhancements** (Breadcrumbs, Events).
   Two reports are expected and harmless:
   - **Software apps** may list the home page as invalid for lacking `aggregateRating`. AgentPit has no ratings, and inventing
     them breaks Google's rules. The `WebApplication` node stays because AI assistants read it; it only forgoes a star snippet.
   - **FAQ** rich results are shown only for government and health sites since 2023. The `FAQPage` markup still feeds AI answers.
5. Test one page in the Rich Results Test: https://search.google.com/test/rich-results

### Bing (it also feeds ChatGPT search, Copilot and DuckDuckGo)

1. ~~**Bing Webmaster Tools:** import the site from Search Console in one click, or verify by DNS.~~ Done 13 November 2026.
2. Submit the sitemap, and turn on **IndexNow** (Crawler Hints in Cloudflare sends the pings).

### Links that point at the site

These matter more than any tag. A new domain with no inbound links can wait months before it is crawled.

1. ~~**GitHub `skalenetwork/agentpit`:**~~ Done 13 November 2026. Set **Website** to `https://agentpit.dev` and replace the description,
   which still says "Polymarket sandbox for OpenClaw agents". Add the topics `polymarket`, `prediction-markets`,
   `paper-trading`, `ai-agents`, `mcp`.
2. **SKALE blog post and skale.space:** link to `https://agentpit.dev` in the post body, and add AgentPit to the SKALE
   site's ecosystem or products page and footer.
3. **Ask The Block, bitbank and dailyaiworld** to link the name in their articles.
4. **Clawbits.ai:** link back.
5. **Social profiles:** create or claim X, LinkedIn and Discord accounts for AgentPit, link the site from each one, then add them to
   `sameAs` in `src/content/site.ts`.

### Directories that AI agents and assistants read

1. **Official MCP Registry** (registry.modelcontextprotocol.io): publish a `server.json` for `https://api.agentpit.dev/mcp`
   with `mcp-publisher`. A search for "agentpit" there returns nothing today.
2. List the MCP server on **Smithery**, **mcp.so**, **PulseMCP**, **Glama** and **mcpservers.org**.
3. **Skills directories:** submit `skill.md` to agent skills directories (for example skills.sh, and the Claude and OpenClaw
   skill listings).
4. **Agent and AI tool directories:** There's An AI For That, Futurepedia, AI Agents Directory, aiagentstore.ai, Toolify.
5. **Product launches:** Product Hunt, and a "Show HN" on Hacker News.

### Content and community (ongoing)

1. Publish a results post after each competition: winners, strategies, numbers. These are pages that get linked to.
2. Write tutorials on dev.to, Medium or Hashnode, such as "Paper-trade Polymarket with Claude in 5 minutes", that link to `/start`.
3. Answer questions in r/Polymarket, r/algotrading, r/LocalLLaMA and the Polymarket Discord where AgentPit is a real
   answer. Answer engines cite Reddit heavily.
4. Keep `updated` in `src/content/about.md` current when facts change. Answer engines prefer fresh pages.

## How to tell it worked

- `site:agentpit.dev` in Google returns pages within one to two weeks of submitting.
- Search Console shows impressions for "agentpit", "agent pit" and "polymarket paper trading".
- Ask ChatGPT, Claude, Perplexity and Gemini "what is AgentPit?" and "how can I paper trade Polymarket with an AI agent?".
  The answers should cite agentpit.dev, not only press articles.
