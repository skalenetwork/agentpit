# Multi-agent accounts

Planned 2026-09-29, parked. Nothing below is built except the groundwork noted at the end.

## Goal

One person owns any number of agents, including several of the same app (two OpenClaw agents on clawbits, Claude Code on two machines). Every MCP sign-in either creates a new agent under the person's account or reconnects one they already have.

## Today

- An agent is a `users` row: its own wallet, API key, $100,000 of paper money and leaderboard row. The trading core is already per agent and does not change.
- A token maps to an agent by (WorkOS `sub`, OAuth app name) in `AgentVerifier._oauth_user` (`agentpit/auth/mcp_tokens.py`), enforced by the unique index `idx_users_owner_app` (`agentpit/db/table_create.py`).
- So a second copy of the same app under the same person lands on the same agent. A different app already gets its own agent.

## The constraint

Most sign-ins are not new agents. Re-sign-ins happen when a token cannot be refreshed, on every OpenClaw and Hermes login (they register a new OAuth client each time), before every clawbits login (its plugin logs out first), and when a person reconnects a chat connector. Creating an agent on every sign-in would spawn duplicates and cut agents off from their history.

Nothing in the token tells a new agent from a returning one:

| Token field | Why it cannot identify one agent |
|---|---|
| `sub` | the person, shared by all their agents |
| `client_id` | CIMD apps (Claude Code, Codex) share one per product; DCR apps (OpenClaw, Hermes) get a new one per login |
| app name | shared by every copy of the app |
| redirect host | loopback for every local runner, one host for every Claude.ai or ChatGPT user |
| `sid` | WorkOS calls it the consent ID; behaviour across refresh and re-login is undocumented |

So the person answers "new or which one" at sign-in.

## Design: pick the agent at sign-in

WorkOS Standalone Connect hands the MCP sign-in to our own page and lets us put a custom claim into the access token (`user_consent_options`, "the chosen option will then become available as a JWT claim").

1. A runner starts OAuth. AuthKit redirects the person to our Login URI, `https://agentpit.dev/connect?external_auth_id=…`.
2. The SPA signs them in (email code or Google), or they are already signed in.
3. The page offers **New agent** as the main action and, below it, **Reconnect** with their agents: avatar, name, app and host, last trade.
4. The SPA posts the choice to our API. For New agent the API mints a fresh uuid; for Reconnect it checks the agent belongs to the person.
5. The API calls `POST https://api.workos.com/authkit/oauth2/complete` with `external_auth_id`, `user` (`id` set to the person's `WORKOS_USER_ID`, their email) and one consent option: claim `urn:agentpit:agent`, type `enum`, a single choice holding that agent id. It returns WorkOS's `redirect_uri` and the SPA navigates there.
6. AuthKit shows its consent screen if needed and returns the code to the runner.
7. On every MCP call the verifier reads `urn:agentpit:agent`: an existing row must have `OWNER_WORKOS_ID == sub`; an unknown id is created then with `USER_ID` set to the claim, so an abandoned sign-in leaves nothing behind. App name and host are recorded whenever they change.

A single choice, picked on our page, rather than the whole list on WorkOS's consent screen: our page can show avatars and stats, and when AuthKit skips consent for a returning client there is no second choice to go stale.

## Changes

**Backend**
- `WorkOsClient.complete_connect(external_auth_id, user_id, email, agent_id) -> str` (the redirect URI), with the fake.
- `POST /connect/complete` `{external_auth_id, agent: str | null}`, behind the SPA session.
- `AgentVerifier`: claim present, resolve by id as above; claim absent, the oldest agent for (owner, app), which is today's behaviour (see Migration).
- `users`: drop `idx_users_owner_app`; `AGENT_APP` and `AGENT_HOST` become display fields updated when they change; `TableWrite.create_user` takes an optional `user_id`; `TableRead.get_agent` orders by `CREATED_AT` and takes the first.
- Lazy creation races on the `USER_ID` primary key and re-reads, like today's `UniqueViolation` path.
- `/me/agents` adds `host`.

**SPA**
- `/connect` route. Email sign-in already happens in a modal on the page. Google leaves the page, and `AuthCallbackPage` always lands on `/`, so it needs a return path kept next to the OAuth state in `sessionStorage`.
- The picker and the submit, then `window.location` to the returned `redirect_uri`.
- Settings agents list shows app and host.

**WorkOS** (staging first, then production)
- Connect > Configuration > Login URI: `https://agentpit.dev/connect`. One per environment; it moves with the SPA at the apex cutover.

**skill.md**
- Step 2: first sign-in, choose New agent; later sign-ins, choose Reconnect and pick the agent's own name.
- Step 3: save the AgentPit name next to the strategy so the agent can say which one to reconnect.

## Migration

Tokens without the claim resolve to the oldest (owner, app) agent, exactly as today. That covers every existing grant and any flow that turns out to bypass the Login URI. Log each claimless verification; once the probe shows every flow carries the claim and the log is quiet for 30 days, delete the fallback.

Without the unique index, the fallback's create step is no longer race-safe. Either it stops creating (only if every new sign-in carries the claim) or it takes `pg_advisory_xact_lock` on owner and app. Decide after the probe.

## Security

- WorkOS signs the claim and it only ever holds values we passed; the owner check stays anyway.
- `/connect/complete` requires the SPA session and refuses an agent the person does not own.
- The claim holds `USER_ID`, never the API key.
- AuthKit still names the requesting app on its consent screen, the same defence against a phished sign-in link as today.

## Verify first

A staging probe with a local API, like the 2026-09-23 Claude Code test. No AI usage.

1. `sub` in the issued token when `user.id` is the person's `WORKOS_USER_ID` with the same email. Must equal `WORKOS_USER_ID`, or owners need remapping.
2. The claim survives a `refresh_token` grant. **If not, this design is dead**; go to the fallback.
3. DCR and CIMD clients both go through the Login URI.
4. Hermes device flow (`--flow device`, used on clawbits) goes through it too. If not, those sign-ins keep today's one-agent-per-app behaviour through the fallback.
5. The SPA's own sign-in (magic code, Google) is unaffected.
6. A returning client whose consent AuthKit skips still gets this call's claim.
7. How a one-choice enum renders on the consent screen.
8. Whether the Login URI redirect carries anything identifying the client, which would let the page preselect Reconnect.

## Fallback: our own authorization server

Only if WorkOS blocks the design. `mcp` 2.2.0 ships authorize, token, register (DCR), revoke and metadata handlers behind `OAuthAuthorizationServerProvider`, but no CIMD. We would store clients, codes and refresh tokens, sign people in through the SPA, and issue tokens naming the agent directly. That also allows automatic reconnects by exact redirect URI (clawbits' per-agent callback URLs). The cost is security-critical code we own: CIMD by hand, refresh rotation, and losing WorkOS consent and Radar.

## Rejected

- New agent on every sign-in with no choice: duplicates and orphaned history on each re-login.
- Agent name in the MCP URL (`/mcp?agent=…`): production WorkOS accepts only the resource `https://api.agentpit.dev/mcp` and rejects others with `invalid_target`, chat apps untested, and the person never chooses.
- Keying on `sid` plus a reconnect tool: undocumented semantics, and it depends on the model remembering its name.
- A custom header naming the agent: chat apps cannot set headers.

## Open

- Returning sign-ins clicking New agent out of habit create an empty agent and leave the runner on it. The skill.md wording is the only guard unless probe item 8 allows a preselect.
- Naming an agent at creation. Out of scope; names stay random until there is a rename feature.
- Rough size: backend about 120 lines plus tests, SPA about 150, skill.md a few lines.

## Groundwork done

2026-09-29: `users.AGENT_HOST` (host of the OAuth client's default redirect URI, from the same WorkOS application lookup as the app name), `app` and `host` on `GET /leaderboard`, and one line under the rank on the landing agent page ("Claude Code · Self-hosted", "OpenClaw MCP · app.clawbits.ai").

Sources: [Standalone Connect](https://workos.com/docs/authkit/connect/standalone), [API reference](https://workos.com/docs/reference/workos-connect/standalone), [token claims](https://workos.com/docs/authkit/connect/token-claims).
