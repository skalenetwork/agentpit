# Launch plan

Working plan after the 2026-07-30 launch rundown with Ben and Ines. Ordered by
what blocks what, not by importance — several things here are more valuable than
the ones above them but cannot start yet.

## Done

**Getting a bot running.** [skalenetwork/agentpit-examples](https://github.com/skalenetwork/agentpit-examples)
is public: a single-file reference agent and the same logic as an OpenClaw skill.
Verified end to end against a local instance — install from the public repo,
prep, reason, finalize, order, fill, position. Five bugs surfaced only by running
it, including the one worth remembering: an agent answering "0.5" for "I don't
know" reads as a 28-point disagreement with a confident market and bets against
it. Abstention is now `probability: null`.

**The builder guide.** The landing page ends in five steps to a running bot —
key, install OpenClaw, add the agent, give it the key, dry run then schedule —
plus the whole thing as one paste. `/get-started` is deleted; the guide had been
duplicated across both pages and had already begun to differ.

**The domain, TLS and the $100k balance model** — all live on
https://agentpit.dev since 2026-08-03. Detail in items 1 and 2 below.

## Next, in order

### 1. Domain and TLS — DONE 2026-08-03

`https://agentpit.dev` and `https://api.agentpit.dev`, certificates from Let's
Encrypt, `www` and plain http both redirecting to the apex. Jack added the two A
records; Caddy issued the certificates itself.

Two things worth keeping, because neither was obvious beforehand.

**`.dev` is on the HSTS preload list baked into every major browser**, so
`http://agentpit.dev` is rewritten to `https://` before a packet leaves the
machine. With 443 unpublished this was not a missing padlock but a site that did
not load at all — while `curl`, which ignores the preload list, returned a
cheerful 200 and made it look like DNS had not propagated. TLS is not optional
on this domain; it is the condition for the domain existing.

**Caddy's certificates now live in a named volume.** They were in the
container's writable layer, which every `build` discards — and a UI deploy
rebuilds that image. Let's Encrypt allows five duplicate certificates a week, so
the sixth UI deploy in a week would have taken the site down with a TLS error.

### 2. Paper balance: 1 quadrillion → $100k — DONE, live since 2026-08-03

Ben's ask, and a prerequisite for anything that ranks accounts.

We looked for a way to avoid a chain redeploy — since we custody user keys,
onboarding could in principle transfer the excess above $100k to a treasury
account and leave the user with exactly that much. We rejected it: with an
arbitrary mint amount the excess never exists in the first place, so there is
nothing to sweep, signup does not pay for an extra transaction, and there is no
second account that now needs its own funding and monitoring. So the faucet
mints a fixed amount baked into the contract, and its minter role cannot be
rotated — the redeploy the treasury idea was meant to dodge turned out to be
the only way to change the number, so we did it properly instead. `drip` is
now operator-only rather than permissionless, which was harmless when every
grant was a quadrillion but would otherwise be a way to route around the daily
cap now that balances are capped at $100k. The faucet gained a second entry
point, `mintTo(address, amount)`, that the operator alone can call for
one-off, non-drip mints.

The user's signup grant is now $100,000 flat, minted the same way it always
was. The house no longer draws from that figure at all — it gets a single
`mintTo` of 10^18 apUSD when the API provisions it at startup, rather than
repeated drips, so the setting that used to keep it topped up is gone. (Not at
deploy time: `deploy_exchange.sh` never mints to the house. `HouseAccountProvisioner._fund`
does, which is also why a wiped chain re-mints on the next boot.) "Refresh balance" is
`POST /me/top-up`: it mints the gap up to $100k, is rate limited to once per
24h by an atomic conditional update on the user's row (so two requests racing
each other can't both mint), and is a no-op that does not spend the day's
allowance if the account is already at or above target. `GET /me/top-up`
reports the same cooldown so the button can show a countdown instead of only
failing after a click.

The chain was redeployed on 2026-08-03 and verified end to end on production: a
fresh signup lands on exactly $100,000, a top-up from $70k mints exactly $30k,
the cooldown then engages, and a stranger's own key gets `only operator` from
both `drip` and `mintTo`. It cost four test accounts and exactly one
human-taker trade — 402,112 of the 402,113 trades in the database were the
liquidity mirror trading with itself.

**The `git submodule update` in that runbook earned itself on first use.** After
`git pull` on production, `git submodule status` showed `+45c190fa` — the `+`
meaning the checked-out commit did not match the gitlink, because a pull moves
the gitlink but leaves the submodule where it was. Without that line `chain-init`
would have compiled the *old* two-argument faucet against the *new* ABI: every
house mint reverts, the API refuses to start, and it surfaces with the chain and
database already destroyed. `deploy_exchange.sh` now compares the two SHAs and
refuses to proceed on a mismatch, so the runbook no longer has to be trusted.

**That decision is now closed, and both halves of it were taken** (2026-08-04,
`docs/superpowers/specs/2026-08-04-net-worth-and-deposits-design.md`). The
top-up used to compare the target against *cash only*, so moving cash into
positions before clicking made you eligible every day — measured live, three
presses reached $400k with no trading involved.

The threshold now counts **cash plus position value**, so the button restores
what was lost rather than paying for shuffling. And a `users.TOTAL_DEPOSITED`
column records what each account has been handed — the signup grant plus every
top-up — written inside the same atomic statement that claims the daily
cooldown, so a mint can never be recorded without its deposit. That is the
second number the leaderboard needs.

### 3. A real leaderboard — three to four days, depends on 2

Today's Arena is a static `leaderboard.json` written by our own bot and shipped
as a frontend asset. No agentpit endpoint ranks anything, so a user who installs
the reference agent never appears.

Both quantities it needs now exist per account: capital is cash plus position
value, and `TOTAL_DEPOSITED` is what the account was handed. That gives four
sortable columns rather than one — which is the point, because different
questions want different orders:

| column | from | answers |
|---|---|---|
| capital | cash + position value | who holds the most |
| earned | capital − deposited | who made the most money |
| return | (capital − deposited) / deposited | who trades best |
| trades | — | who is actually active |

**Default to return, not capital.** The default sort is what "the leaderboard"
means, and capital alone rewards whoever pressed the top-up button most.

**That precondition is lifted.** `TOTAL_DEPOSITED` used to be wrong after a
chain wipe, because the database outlives a disposable anvil. It is now keyed
to the deployment: every redeploy writes new contract addresses, the account
records which one its figures belong to, and a mismatch resets the ledger
exactly once. No native-balance guess, no RPC, and nothing that can fire twice.

**Only accounts that have traded appear** — the natural filter, and it sidesteps
putting every registered address on a public board by default.

This had to follow the balance change. While everyone started with a quadrillion
and traded in hundreds, the differences vanished into rounding and the board
would have ranked noise.

Compute it on a timer, not on read. `list_positions` reads the chain per
account, and pagination does not rescue that: to know who belongs on page one
you must value everyone, so paging limits what is sent and not what is
computed. A background pass writes each trading account's value to a column and
the endpoint becomes an ordinary `ORDER BY … LIMIT` — at which point paging is
free. `SnapshotService` already does exactly this shape for market mids, on a
timer, and is the thing to copy. It also fixes a second problem: the page polls
every four seconds, which on-read would mean walking every account, on-chain,
four times a minute, per open tab.

Every account on the board is an ordinary row. An earlier draft badged our five
personalities as ours and keyed the badge to the handle — a field its subject
can edit — so official status would have been claimable by anyone who set the
right name. Dropped.

**Deploy checklist: the valuation pass has to be turned on.** The background
pass above is opt-in, defaulting to off, gated by `AGENTPIT_LEADERBOARD_ENABLED`.
The only place it is set today is `deploy/env.prod.example`, a template a human
copies once to the server's `.env`; the file does not update itself when the
template gains a line after that copy already happened. Miss it and
`account_snapshots` never gets a row — the board renders the exact empty state a
brand-new site is supposed to show, so a trader who never appears looks like
nobody has traded rather than like a broken deploy, and nothing but a log line
says otherwise. Confirm `AGENTPIT_LEADERBOARD_ENABLED=true` is actually present
in the server's `.env` on every deploy, the same way `SNAPSHOT_ENABLED` needs
its own check — both are opt-in background-pass flags that default to off and
raise no error when forgotten, only an absence that reads as normal.

### 4. Documentation — five to seven days, independent

Self-hosted at `agentpit.dev/docs`, no vendor. Guides in Markdown; the API
reference generated from the OpenAPI schema the backend already serves, so it
cannot drift. Measured before planning this: 42 of 43 endpoints documented, zero
stale entries — the reference exists and is accurate, it is only invisible.

Can run in parallel with anything above it.

## Blocked on other people

- ~~**Jack** — the two DNS records.~~ Done 2026-08-03; both resolve.
- **Ben** — whether the launch targets developers or a mass audience. The
  written positioning says "the development platform… engineers use it"; the
  rundown said DGEN traders who will not read documentation. The two lead to
  different products, and the question worth putting to him is what a
  non-technical visitor gets out of paper money that brings them back tomorrow.
- **Ines** — landing copy, beta label and the SKALE section are merged and live
  (branch `changes-landing-page`, 2026-08-03). Still outstanding: demo video and
  logo. Her "Copy section" button was dropped in the merge — it assembled the
  four curl steps and the single-file agent, all of which went when the guide
  became the five OpenClaw steps, and `setup.sh` already gives that section a
  copy-everything affordance that is also runnable.

## Deliberately not doing

**Hosting agents for users.** Each user runs their own reasoning on their own
machine; we do not hold anyone's model key and we are not a compute provider.

**A chat that trades.** The interface already does it in two clicks, and better.

**MCP.** It serves conversational trading, which is the thing above.

Reversed 2026-09-23: agents now join through MCP, see [agent-onboarding.md](agent-onboarding.md).

### 5. Move onto SKALE — a launch blocker now, not a footnote

This used to sit at the bottom as "before a real chain, someday". It moved up on
2026-08-03: the landing page now carries a SKALE section whose roadmap lists
"Zero-fee paper trading on SKALE" under **Live now**. We are on a local anvil,
chain id 31337, in Docker. The claim becomes true only after this migration, so
it has to happen before anyone sees the page.

**The operator key is a public one.** `ADMIN` is
`0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266` — account #0 from every Foundry
install, its private key printed in the docs and sitting in `env.prod.example` in
plain text. That is correct for a paper chain and catastrophic on a durable one:
it is the faucet operator and the exchange admin. Generate a fresh key before the
first SKALE deploy. Nothing will warn you — tests pass, deploys succeed, and the
gate simply admits the whole internet.

**`AGENTPIT_SIMULATED_CHAIN` must be set false.** Production does not set it
today, so it defaults true. It gates re-onboarding on a zero native balance,
which on a disposable chain means "the chain forgot this account" and on a
durable one means "this account spent its gas" — an ordinary state, reachable
deliberately. Left true, login mints a fresh gas grant on demand, repeatedly.

**The gas grant is sized for anvil.** `signup_gas_grant_wei` is 10^18 — one
whole native token per user. SKALE's native token is sFUEL, distributed in tiny
amounts. A thousand whole tokens is not a budget the operator will have.

**It is another destructive redeploy.** New chain, new CTF, new ERC-1155 token
ids, another database reset. Today that costs a handful of test accounts. After
launch it costs users their track record — so the move happens *before* anyone is
invited, not after.

The one thing this migration makes *better*: on a normal chain the operator pays
gas to settle every fill, which grows linearly with activity and would be a real
cost. SKALE has none, so of all the durable chains it is the one where the
settlement model stays free.
