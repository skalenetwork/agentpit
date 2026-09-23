# AgentPit landing: initial phase plan

Prepared 21 September 2026. Every number here was measured against the live API or read out of
installed package source, not estimated.

**Status:** sections 1 to 9 are decided. The `web/` scaffold, the home page and `/start` are built and
build clean, with the three deviations noted in section 12. The visual design is NOT done: the first
pass is a placeholder and section 13 is the next piece of work. Nothing is committed.

---

## 1. The shape

**The boundary is authentication, and nothing else.**

`agentpit.dev` is the public product, rendered by Astro as HTML with no client framework. Every URL a
person can use without an account lives there. `app.agentpit.dev` is the console, owns no public path,
mirrors no public URL, and is disallowed in robots wholesale. `api.agentpit.dev` does not move.

Two properties this buys, which are the two things that make a split feel awkward when they are missing:

- One URL per thing. A market is a document on the apex and a workspace in the console, and the console's
  has no public twin, so there is never a canonical fight or a second design of the same page.
- Phase 1 is a strict subset of the end state. Every later addition is a new file under a pattern that
  already exists, not a re-architecture.

Rejected shapes, with why: **brochure** (marketing plus docs only, fully static) forfeits every data
surface and cannot put live books on the front page; **market first** (the apex is the grid) leads with
the half of the product a bot developer cannot act on; **progressive shell** (one URL that upgrades in
place when signed in) puts personalisation and edge caching in direct conflict; **one codebase** (retire
the SPA, Astro serves everything) is the right end state but pays for a console rewrite before any of
the value lands.

### Measured facts the shape rests on

| Fact | Value |
|---|---|
| Markets total / closed / open | 5,641 / 4,147 / 1,494 |
| Open with a two-sided quote | 1,174 |
| Open, two-sided, `endDate` in the future | **1,098** |
| Events total / single-market / multi-market | 1,105 / 546 / 559 |
| Single-market events whose slug equals their only child's | 487 |
| Multi-market events with at least one open market | 168 |
| Board accounts / with 10 or more trades | 16 / 9 |
| API cache headers | none, on any endpoint |
| API latency, cold, from a laptop | `/events/{slug}` 69ms, `/book` 283ms, `/markets?slug=` 403ms |
| Catalogue sync interval | 3600s |
| Quote mirror interval | 2.0s |
| Board refresh / snapshot interval | 300s / 900s |

---

## 2. End-state route table

Ownership and indexability for the whole surface. Phase column says when it ships.

### agentpit.dev, Astro, `output: 'static'` with per-route `prerender = false`

| URL | Renders | Indexable | Edge cache | Phase |
|---|---|---|---|---|
| `/` | on demand | yes | 60 / 300 | 1 |
| `/start` | prerendered | yes | asset layer | 1 |
| `/docs` | prerendered | yes | asset layer | 1 |
| `/docs/[...slug]` | prerendered | yes | asset layer | 1 |
| `/docs/[...slug].md` | prerendered, `text/markdown` | yes, `Link: rel=canonical` header | asset layer | 1 |
| `/llms.txt`, `/llms-full.txt` | prerendered | yes | asset layer | 1 |
| `/robots.txt` | prerendered | n/a | asset layer | 1 |
| `/sitemap.xml`, `/sitemap-pages.xml` | prerendered | n/a | asset layer | 1 |
| `/og/[kind].png` | prerendered | n/a, `noindex` | immutable | 1 |
| `/404` | prerendered | no | asset layer | 1 |
| `/markets` | on demand | yes | 60 / 300 | 2 |
| `/markets/page/[n]` | on demand | 2 and 3 yes, deeper `noindex, follow`, all self-canonical | 60 / 300 | 2 |
| `/markets/[slug]` | on demand | only open, two-sided, future `endDate`: 1,098 of 5,641 | 30 / 120 | 2 |
| `/events/[slug]` | on demand | multi-market with an open child: 168 | 60 / 300 | 2 |
| `/og/market/[slug]-[hash].png` | on demand | n/a, `noindex` | immutable | 2 |
| `/sitemap-markets.xml`, `/sitemap-events.xml` | on demand | n/a | 3600 / 21600 | 2 |
| `/agents` | on demand | yes | 120 / 600 | 3 |
| `/agents/[address]` | on demand | `trades >= 10`: 9 today | 120 / 600 | 3 |
| `/legal/terms`, `/legal/privacy` | prerendered | yes | asset layer | when auth consent needs them |

Browser `Cache-Control` is `public, max-age=0, must-revalidate` on every document, set in middleware. A
private cache cannot be purged; the edge can.

### Redirects, all 301, query string preserved

`www` to apex. `/events/[slug]` with a single market to `/markets/[slug]` (546 events, 487 slug-identical).
`/markets/[int]` to `/markets/[slug]`. `/profile` to `app/portfolio`. `/settings` to `app/settings`.
`/auth/callback` to `app/auth/callback`, kept permanently for in-flight sessions. Trailing slash to none.

### app.agentpit.dev, existing Caddy host, SPA

`X-Robots-Tag: noindex, nofollow` on every response, `robots.txt` is `Disallow: /`. Console browse lives
at `/trade`, deliberately not mirroring `/markets`, so a signed-in session never bounces between origins.

---

## 3. Phase 1 scope

Home, the developer path, docs, and every machine surface. No market pages, no event pages, no board
pages. The home page carries live proof, which is the one reason the adapter is in from day one.

**Ships**

1. `/` with the positioning, a runnable curl, a live market strip and the top of the board as proof.
2. `/start`: swap the base URL, get a key, read a book, place an order, check holdings.
3. `/docs` index plus `/docs/api` and `/docs/polymarket-compatibility`, each with a `.md` twin.
4. `robots.txt`, `sitemap.xml` and `sitemap-pages.xml`, `llms.txt`, `llms-full.txt`, real `404`.
5. Baked OG cards for home, start and docs.
6. The full redirect table and the origin split.
7. CI, from nothing: the repo has no `.github` directory today.

**Deferred, and nothing deferred forces a migration**

Market, event and board pages are new route files under the same adapter, the same cache vocabulary and
the same index-floor predicate. Tag pages are phase 2 and are rendered as plain text in phase 1, never as
links, so no URL is promised and then moved.

**Five things that cannot be retrofitted, so they all ship in phase 1**

The origin split. The canonical and redirect rules. The cache-tag vocabulary, because a response cached
without tags is unpurgeable for its whole TTL. The index-floor predicate. And the invariant that no
per-user byte ever appears in cacheable HTML.

---

## 4. File tree

```
agentpit/
  web/
    astro.config.ts
    wrangler.jsonc
    tsconfig.json
    package.json
    bun.lock
    .gitignore
    public/
      _headers
      favicon.svg
      fonts/geist-mono-500.woff        satori needs .woff, never .woff2
    src/
      content/
        site.ts                        name, base URLs, nav, footer, affiliation line
        home.ts                        every string on the home page, as typed records
        measurements.ts                every sampled figure with its asOf date and method
        docs.ts                        the publish allowlist, order, and the build guard
      lib/
        api.ts                         typed public fetchers, one per endpoint
        gamma.ts                       the two wire gotchas, encoded once, unit tested
        og.ts                          one typed card builder, fed by both renderers
      components/
        Layout.astro                   head, canonical, meta, OG, JSON-LD slot, font, skip link
        Header.astro                   wordmark plus Docs, App, GitHub
        Footer.astro
        Section.astro                  the only layout primitive: rule, heading, lead, body, proof
        Code.astro                     one language per block, optional label, copy button
        Facts.astro                    typed { term, def }[] as a hairline two-column dl
        Figure.astro
        Stamp.astro                    an "as of" line, so no figure renders without its date
        DocsNav.astro
        MirrorPath.astro               inline SVG: the pull-only path, dashed no-write-back arrow
      pages/
        index.astro
        start.astro
        404.astro
        docs/
          index.astro
          [...slug].astro
          [...slug].md.ts
        og/
          [kind].png.ts
        llms.txt.ts
        llms-full.txt.ts
        robots.txt.ts
        sitemap.xml.ts
        sitemap-pages.xml.ts
      styles.css
      middleware.ts                    browser Cache-Control, security headers
  .github/
    actions/setup-bun/action.yml
    workflows/web.yaml
  docs/
    API.md                             edited: public base URL, the four factual fixes
    polymarket-compatibility.md        new, endpoint-by-endpoint parity table
```

`web/.gitignore` is not optional: the repo root `.gitignore` does not ignore `node_modules` at all, so
without it the first `bun install` leaves thousands of untracked files in a repo committed by hand.

`.git/info/exclude` is currently empty. `.claude/` goes in it before any `.claude/launch.json` exists.

---

## 5. Dependencies, pinned exact

**dependencies**

```
astro                  7.3.3
@astrojs/cloudflare    14.3.2
tailwindcss            4.3.3
@tailwindcss/vite      4.3.3
```

**devDependencies**

```
typescript             ~6.0.3
@astrojs/check         0.9.10
@types/node            26.6.2
wrangler               4.136.0
@cloudflare/workers-types  5.20260921.1
satori                 0.33.4
@resvg/resvg-js        2.6.2
@types/react           19.3.0       types only, for satori's ReactNode signature
```

Not installed, deliberately: **React and shadcn** (the landing has no component library and no islands;
the UI kit belongs to the console). **`@astrojs/sitemap`** (it cannot emit on-demand routes and names its
own index `sitemap-index.xml`, so keeping it means two sitemap systems and a filename collision; ours is
about 30 lines). **`@astrojs/markdown-satteri`** (astro 7.3.3 already pins 0.4.1 exactly and only a
configured `markdown.processor` needs it declared). **`@cf-wasm/og`** until phase 2.

**Version notes that are not obvious**

- `typescript` stays on `~6.0.3`. TS 7.0.2 is released and is the Go compiler, but it ships without the
  stable programmatic API that lands in 7.1, and `@astrojs/check@0.9.10` peers `^5.0.0 || ^6.0.0`
  outright. Revisit in October.
- Base UI's live package is **`@base-ui/react`**, at 1.8.0, stable since its 1.0.0 in December 2025.
  `@base-ui-components/react` is the pre-rename package, frozen at `1.0.0-rc.0`. shadcn selects it via
  `components.json`'s `style` field with a `base-*` preset, or `init -b base`. Relevant to the console,
  not to this site.
- If React is ever added here, it must be `@astrojs/react@6.0.6`, not the `^5` the shadcn Astro template
  pins: `5.x` depends on Vite 7 while astro 7.3.3 depends on Vite 8.
- `bun run build` already executes Astro under Node through its bin shebang, verified empirically on this
  machine. So bun installs and drives while Node executes. Never `bun --bun`: that puts Astro on Bun's
  runtime, which is where the Cloudflare adapter's open issues live.

---

## 6. Configuration

### astro.config.ts

- `site: process.env.SITE_URL ?? "https://agentpit.dev"`.
- `output: 'static'` with per-route `export const prerender = false`. Not `output: 'server'`: that
  inverts the default and makes every static page a billed Worker invocation for nothing.
- `adapter: cloudflare()` with a top-level `session: false` (it is an Astro option, not an adapter
  option). **`session: false` is load-bearing.** Read in 14.3.2's own
  `dist/index.js` and `dist/wrangler.js`: with sessions left at default the adapter assigns the
  Cloudflare KV session driver and emits `kv_namespaces: [{ binding: "SESSION" }]` into the Wrangler
  config, which must then be provisioned in Cloudflare before a deploy succeeds. The apex has no
  sessions by design.
- `trailingSlash: 'never'` with `build: { format: 'file' }`. Fixed before the first href, canonical or
  sitemap entry is written. `format: 'file'` is also what lets the `.md` twins sit beside their pages
  without a directory collision.
- `cache: { provider: cacheCloudflare() }` plus top-level `routeRules`. `routeRules` is a sibling of
  `cache`, not nested inside it, and a rule takes exactly `maxAge`, `swr`, `tags`. The provider emits
  `Cloudflare-CDN-Cache-Control` and `Cache-Tag`, adds an automatic `astro-path:` tag, and does not
  touch `Cache-Control`, so the browser-facing header stays ours and per-URL purge is free.
- `security: { csp: { algorithm: 'SHA-256' } }`, leaving script-src and style-src to Astro so it can
  inject its own hashes. Cloudflare Web Analytics needs its script host allowed and its collector host
  in `connect-src`, and those are two different hosts: verify against the live beacon once deployed
  rather than assuming, because a missing collector host is a silent zero, not a visible error.
- `devToolbar: { enabled: false }`.
- Fonts through the built-in API: Geist Mono, weights 400 and 500, `fontProviders.fontsource()`,
  `fallbacks: ["monospace"]`.
- No `experimental.incrementalBuild` in phase 1: it caches into `node_modules/.astro`, so it is worthless
  in CI unless the cache step restores `node_modules`, and it is still an experimental flag.

### wrangler.jsonc

Minimal, because the adapter fills in `main`, the `ASSETS` binding, `compatibility_date` and
`cache.enabled` itself. Inspect the first build's output before the first deploy.

- `"$schema": "node_modules/wrangler/config-schema.json"`.
- `assets.not_found_handling: "404-page"`, which is a silent no-op without `src/pages/404.astro`.
  **Never** `"single-page-application"`: that is exactly the soft-404 origin we are leaving.
- `assets.html_handling: "drop-trailing-slash"`, to agree with `trailingSlash: 'never'`.
- `"workers_dev": false`. Without it the Worker gets a public `*.workers.dev` origin serving a second
  fully indexable copy of the site.
- **No `routes` key, ever.** Today it would fail outright, since the zone is not on Cloudflare. After the
  zone moves it would make the first deploy perform the DNS cutover as a side effect. The hostname is
  attached by hand in the dashboard, once.
- `env.staging` and `env.production`, and CI always passes `--env`. A bare `wrangler deploy` with `env`
  blocks present creates a third nameless Worker.

### CI

`.github/workflows/web.yaml` plus `.github/actions/setup-bun/action.yml`, adapted from clawbits. The
workflow cannot be copied as a drop-in: it depends on that composite action, which does not exist here.

Shape: `main` builds and deploys staging, `prod` builds and deploys production, pull requests build and
verify but never deploy. Pinned Node 24 alongside bun. `bun install --frozen-lockfile`, so `bun.lock` is
committed first. A `paths` filter covering `web/**`, `docs/**` and the workflow itself, matching whatever
`vite.server.fs.allow` permits. `permissions: contents: read`. A concurrency group per ref, cancelling
pull request builds but never a deploy in flight.

Two guards worth copying verbatim in spirit:

- **Two-directional indexability assert.** If the resolved host is the production apex, fail when
  `robots.txt` carries `Disallow: /` and fail when the built canonical does not match `SITE_URL`.
  Otherwise fail when it does not. The one-directional version of this check ships a silently
  de-indexed launch with CI green.
- **Link integrity.** Every allowlisted doc exists, every internal doc link resolves to a published URL,
  every `llms.txt` link returns 200.

Repository secrets to add by hand: `CLOUDFLARE_API_TOKEN` scoped to Workers Scripts: Edit, which is
exactly what permits leaving `routes` undeclared, and `CLOUDFLARE_ACCOUNT_ID`.

---

## 7. Sequencing

### Phase 0: build against a preview host. Zero production risk.

Scaffold `web/`, build the site, deploy to `preview.agentpit.dev` or a `workers.dev` URL. Nothing in the
existing stack changes. All design and copy work happens here while the domain question stays open.

### Phase 0b: move the zone to Cloudflare. Its own change, its own rollback.

`agentpit.dev` is on GoDaddy nameservers (`ns27`/`ns28.domaincontrol.com`) pointing at `23.88.62.130`,
where Caddy terminates TLS for both the apex and the API. A Worker cannot own the apex until this moves.

1. Create the zone. Import all records: apex A `23.88.62.130`, api A `23.88.62.130` **DNS-only**, www.
2. Verify with `dig @<assigned-cf-ns>` before touching GoDaddy.
3. Flip the nameservers. Wait for activation and Universal SSL. Deploy nothing.
4. Verify the apex and the API still serve byte-identically.

`api.agentpit.dev` must stay DNS-only. Proxying it breaks Caddy's ACME HTTP-01 challenge, and `.dev` is
HSTS-preloaded, so there is no http fallback: the API goes dark rather than losing a padlock. DNS-only
also preserves the real client IP that the per-IP rate limit on `/auth/code` depends on. This is the
riskiest half hour of the project and it should not share a day with a code deploy.

### Phase 0c: stand up `app.agentpit.dev` while the apex still serves the SPA.

1. DNS A record, DNS-only.
2. Add an `app.agentpit.dev` block to `deploy/Caddyfile`, with `X-Robots-Tag: noindex, nofollow`.
3. **Rebuild the caddy image.** The Caddyfile is baked in at `deploy/Dockerfile.ui:41`, not mounted, so a
   restart keeps the old config.
4. Add `https://app.agentpit.dev` to `AGENTPIT_CORS_ORIGINS`, keeping the JSON array syntax: a
   comma-separated value fails startup.
5. Register `https://app.agentpit.dev/auth/callback` in WorkOS alongside the existing URI, before
   anything moves.

Both hosts now work and nothing has moved.

### Phase 0d: SPA cutover.

Point `VITE_WORKOS_REDIRECT_URI` at the new callback, delete the SPA's landing page, make its `/`
redirect into the console, and rebuild: a restart leaves the old redirect URI inlined in the bundle.

**Announce a one-time sign-out.** Tokens live in `localStorage` on `agentpit.dev` and the backend sets no
cookies, so no cookie-domain trick can carry sessions across. Do not build a fragment token handoff for
this.

### Phase 0e: apex cutover.

Attach the apex and www to the Worker as custom domains, by hand, in the dashboard.

### Then: submit nothing for a day.

Deploy, then watch `cf-cache-status` and the uvicorn request rate for a full day before submitting any
sitemap. Roughly 1,300 on-demand URLs against one uvicorn that sends no cache headers at all is the
project's first real load event, and the fix for a burst is raising `swr`, not adding capacity.

---

## 8. Invariants

Rules a reviewer can apply without a judgement call.

1. No em dashes or en dashes anywhere, including alt text.
2. Sentence case everywhere. No all-caps anything.
3. Every figure carries a unit, an as-of date, and a sample size when it is a sample. Figures are read
   from `measurements.ts`, so a stale stamp is visible rather than silent.
4. Every limit lives with its claim, same block, same type size. No asterisks, no footnote-only
   disclosures, no trailing-clause retractions.
5. No proof device a reader cannot reproduce with curl, and none whose values were hand-written.
6. No auth-dependent content on the apex. The landing never renders a key, a balance or a signed-in state.
7. No per-user byte in cacheable HTML.
8. No query parameters on any public URL. Pagination is a path. One canonical ordering per route.
9. No document published that has not been verified current.
10. Geist Sans everywhere, Geist Mono only inside `pre` and `code`. Weights 400 and 500 only.
11. One hairline border, two radii by role (`control` pill, `panel` 24px), one fill. No gradients, no
    shadows, no tinted panels. The only container is the Panel primitive.
12. One accent, used for link text, the focus ring, and one word in the H1. Nothing else.
13. One motion instance, guarded by `prefers-reduced-motion`. No scroll reveals, no count-ups.
14. No icons as decoration, no stock illustration, no mascot, no logo wall, no testimonial row.
15. No inline code comments in shipped source.
16. Total client JS on the apex is the analytics beacon. State that rather than claiming zero.

### Banned claims, by name

"Polymarket's own CTFExchange" (`vendor/ctf-exchange` points at `github.com/yavrsky/ctf-exchange`, a
fork, and the submodule is not checked out). "Deployed from source" (ConditionalTokens is deployed from
committed creation bytecode, `scripts/deploy_exchange.sh:120-134`). "Identical Polymarket mechanics",
"the bytes match", "same contracts, same settlement". Any market-level volume or trade-count figure.
"Risk free". "Sandbox" or "playground" as a noun for the product.

### Facts that go on the page rather than being found unaided

We hold the user's private key. Of a 40-market sample, 23 were two-sided; of 60, 9 had any trade tape;
the worst mirrored midpoint was 4.3 cents stale.

### Launch gate: sample strategies

The "Starter strategies" card on the home page promises a choice of open-source sample strategies. On
21 September 2026 `skalenetwork/agentpit-examples` holds one reference agent, not several strategies, so
the card must not ship before the examples repo offers the choice. The copy names no count on purpose.

The three strategies the card's visual names, chosen 21 September 2026 because each is a plain rule over
endpoints that already exist:

- **Favorites** (handshake, risk 1): back the crowd. Buy the outcome already priced above about 85 cents
  on markets close to their end date and hold to payout. Needs `GET /markets` only.
- **Momentum** (candlestick chart, risk 2): ride the move. Read `GET /prices-history`, buy outcomes whose
  price has risen by a set amount over the last day, exit when it reverses.
- **YOLO** (skull, risk 3): long shots. Spread small stakes over outcomes priced under about 5 cents; most
  expire worthless and a hit pays twenty to one or better.

The existing reference agent, a model that forecasts a probability without seeing the price, is a fourth
candidate and the natural "bring your own model" option; the deck shows three.

### Launch gate: SKALE on Base

Decided 21 September 2026: the landing launches with production live on SKALE on Base (chain id
1187947933, RPC `https://skale-base.skalenodes.com/v1/base`, explorer
`https://skale-base-explorer.skalenodes.com`). The hero, the contracts heading, the chain row in the
limits section, the `/start` order note, the meta description and the footer explorer link all say so.
On the day this was written production still ran a single-node chain at id 31337, so the landing must
not go live before the backend cutover, or every one of those lines is false.

---

## 9. Content

### Positioning

AgentPit is a prediction market exchange that mirrors live Polymarket order books and settles every fill
on its own chain, in real ERC-1155 outcome tokens against paper apUSD collateral.

The one claim the page proves: **a fill here is a transaction, not a return value.** H1 accordingly, with
`transaction` as the one accented word.

Three supporting claims, each with its proof: the machinery is real (a recorded `POST /order` whose
response carries a real `transactionsHashes`); the book is not ours (level-count diff across both venues
with a methodology footnote); nothing is at stake and the bill for that is on the page (the divergence
table, including the two facts that look worst if a reader finds them unaided).

### Docs

Phase 1 publishes `/docs` plus `/docs/api` and `/docs/polymarket-compatibility`. Phase 2 adds
trading-model, orders, settlement and a reference agent, each of which has to be written rather than
moved.

`docs/API.md` cannot be globbed as-is. Its curls point at `http://localhost:8000`, it documents a
`400 MarketStateError` that does not exist, it says FOK and FAK work when they rest like a GTC, it
advertises a `/get-started` page that was deleted, and it covers 36 of the 49 live paths. The reference
becomes a committed snapshot of the live OpenAPI schema plus a refresh script, with hand-written prose
around it. No build depends on production being up.

Internal, fact sources only, never published: `ONBOARDING.md` (it has a known-bugs table),
`overview_for_investors.md`, `agentpit_whitepaper.md`, `launch-plan.md`, `agentpit_api.md`,
`high_level_design.md`, both simulator and CTF spec files, `polymarket_sync_spec.md`, `tests_overview.md`,
`missing_features_for_mvp.md`, `missing-features/**`, `news_parsers.md`. Four of them still name
agentpit.ai as the product URL and two describe a SQLite design with no blockchain, so the allowlist is a
freshness gate, not only a leak gate.

### Design system

Set on 21 September 2026, with aave.com as the layout reference. Geist Sans for the whole interface,
Geist Mono only inside `pre` and `code`, weights 400 and 500. Six type steps: display, heading,
title, lead, body, small. Six colours taken from the console's slate theme, each a `light-dark()` pair: paper,
surface, ink, muted, line, signal (`#2563eb` light, `#60a5fa` dark). Two radii by role: `control` for
every control, `panel` for every surface. One Button: sizes `sm` and `lg`, variants primary, secondary
and ghost, press feedback guarded by `motion-safe`. One Panel primitive: surface fill, no border, no
shadow. Icons from Lucide only, installed when first needed. `global.css` resets Tailwind's default
colour, text, radius, weight and container namespaces, so off-system utilities do not exist.

### On llms.txt

Shipped because it is about 30 lines, not because it works. Ahrefs, May 2026: of 137,000 sites serving
one, 97 percent got zero traffic from it. SE Ranking across roughly 300,000 domains found no significant
correlation with AI citations. Google has stated it has no support and no plans, and crawlers do not probe
for the file on domains that lack it. The thing that actually works is the `.md` twins: real URLs an
assistant can fetch and quote, served as `text/markdown` with a canonical `Link` header, and not
`noindex`, because that would tell AI crawlers to drop the artifact built for them.

---

## 10. Decisions needed

| Decision | Recommended default | Cost of the alternative |
|---|---|---|
| Phase 1 scope | Home, start, docs, machine surfaces. Market and event pages in phase 2, board in phase 3. | Including market pages now adds 1,098 indexable pages and the runtime OG route to the first ship. |
| Tailwind 4, or plain CSS | Tailwind 4, as chosen, with a `@theme` block holding exactly the token set above. | reef ships plain CSS for a site this shape. For eight tokens and five type steps, plain CSS is less code. |
| Cloudflare account | The one holding clawbits and reef. | A second account to administer, and a second billing relationship. |
| Workers plan | Paid, 5 dollars a month. | Free is 10ms CPU per invocation, which a runtime OG render cannot survive, and 100k requests a day. |
| Market page content | Our book, our mid, spread in cents, both `clobTokenIds`, `conditionId`, tick size, min order size, a copyable curl. No upstream description, no hotlinked upstream icon. | Republishing the upstream description is literal duplicate content: it is byte-identical for 1324 characters, and all 1104 event icons currently hotlink Polymarket's S3 bucket. |
| `ui/` from yarn 4 to bun | At the console rewrite, not now. Phase 1 shares no code with the SPA. | Doing it now is the cleaner workspace; doing it never is the drift vector. |
| Move `ui/`'s market parse helpers into a shared package | Not yet. Encode the two wire gotchas once in `web/src/lib/gamma.ts`, unit tested with `bun test`. | `outcomePrices` arrives as the string `'["0.9","0.1"]'` and `bestBid`/`bestAsk` use `0` to mean no resting order. A reimplemented parse prints "0c" where a page should read "no bid". |

### One blocking artifact

Someone has to capture a real authenticated `POST /order` that returns `matched` with a genuine
`transactionsHashes`, hashes truncated for width. If it cannot be captured, the hero artifact becomes the
public `GET /book` response, which any reader can reproduce without an account, and the fill moves down a
section. Nothing illustrative ships either way.

### Two quick wins in the existing stack

`api.agentpit.dev` serves no compression at all: 2.38 MB uncompressed for 100 events. One `encode gzip
zstd` line in the API block of `deploy/Caddyfile`. And the API has no `robots.txt` while returning 200
JSON at `/`.

---

## 11. The honest caution

The board has 16 accounts, 9 of them with 10 or more trades. If the real bottleneck is that the console
is unpleasant rather than that nobody can find the product, then this work is a beautiful front door onto
an app that still needs the rewrite, and the awkward hybrid comes back in a different form.

The cheapest signal either way, at eight weeks: arrivals on `/start` converting to new accounts, and the
count of accounts above 10 trades. If the board is still 16 and 9, the next block of work belongs in the
console, and this shape has already paid for itself by not touching it.

---

## 12. Built, and where it deviates from this plan

Built on 21 September 2026: `web/` scaffold, `/`, `/start`, `404`, `robots.txt`, `sitemap.xml`, and the
Layout, Header, Footer, Section, Code, Facts and Stamp components, with all copy in typed content
modules. `astro check` reports 0 errors, the build ships zero JS, and the payload is 8 KB of CSS plus
24 KB of self-hosted Geist Mono.

Three deviations from section 6, each deliberate and reversible:

1. **The adapter landed with the first live route.** `/` is `prerender = false`: it fetches
   `GET /markets/stats` through `src/lib/api.ts` with a 2 second timeout and falls back to the snapshot in
   `measurements.ts`, so the page never depends on the API being up. `routeRules` caches it at the edge
   for 60 seconds with 300 of stale-while-revalidate. Verified on the built Worker: the response carries
   `Cloudflare-CDN-Cache-Control: public, max-age=60, stale-while-revalidate=300` and
   `Cache-Tag: astro-path:/`, and the generated `dist/server/wrangler.json` has no KV binding. Every other
   page is still prerendered. The browser `Cache-Control` middleware from section 2 is not built yet.
2. **No `env` blocks in wrangler.jsonc.** With a single target they only add the footgun where a bare
   `wrangler deploy` creates a nameless Worker. They arrive with the staging host.
3. **`markdown.syntaxHighlight: false`** added, because Shiki emits inline styles that the CSP refuses.
   This matches the design decision to style `pre` by hand in one tone.

Facts on the page and their sources: 1,489 active markets from `GET /markets/stats`; `0 bps` from
`feeRateBps=0` at `agentpit/services/order_service.py:156`; `$100,000` from
`AGENTPIT_PAPER_BALANCE_TARGET_RAW` at `agentpit/config.py:353`; the hero's order book is a real
`GET /book` response captured the same day, 10 bids and 10 asks at 0.14 bid and 0.16 ask on a 0.001
tick. The recorded `POST /order` artifact named in section 10 was never captured, so the page uses the
public book response, which is the documented fallback.

## 13. The design pass, in progress

Worked piece by piece, each piece proposed and confirmed before it is built.

1. Done: tokens, fonts and Button, per the design system in section 9.
2. Done: page structure, a 1:1 copy of the blocks on aave.com/app and aave.com/pro (986px container,
   100px sections, six type steps), filled with the existing copy. Visual slots are empty placeholders.
3. Next: adapt the blocks to our user journey, design the visuals, and give `/start` the sticky steps
   layout.

## 14. Captured artifacts, kept for the hero visual

A real public `GET /book` response, captured 21 September 2026. It was on the home page as code and was
taken out when that section became the feature grid. It is the raw material for the hero visual, so it
lives here rather than as unused code.

```ts
export const book = {
  question: "Zhang Youxia sentenced to prison before 2027?",
  tokenId: "59839879650270790460093635610916900568022466348035211317118010229597833976834",
  bestBid: "0.14",
  bestAsk: "0.16",
  bidLevels: 10,
  askLevels: 10,
  tickSize: "0.001",
  asOf,
} as const;

export const bookRequest = `TOKEN=${book.tokenId.slice(0, 12)}...
curl -s "${site.api}/book?token_id=$TOKEN"`;

export const bookResponse = `{
  "market": "0x704dde6d56ef926c949eafa7f28492feb6a1786b94dacc20889d9900110a332c",
  "asset_id": "59839879650270...9783397683",
  "timestamp": "1790001207697",
  "bids": [
    { "price": "0.14", "size": "290" },
    { "price": "0.13", "size": "890" },
    { "price": "0.12", "size": "5557.27" }
  ],
  "asks": [
    { "price": "0.16", "size": "100" },
    { "price": "0.17", "size": "20" },
    { "price": "0.18", "size": "257.37" }
  ],
  "tick_size": "0.001",
  "neg_risk": false
}`;
```
