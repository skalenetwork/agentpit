# Missing Features for MVP

**Date:** April 28, 2026

Five features stand between the current codebase and a shippable MVP. All five are required. None are optional.

---

## 1. Order Endpoint Surface: gaps

The core order endpoints exist today against `OrderService` (`POST /orders`, `DELETE /orders/{order_id}`, `GET /orderbook/{market_id}/{outcome}`, see `agentpit/api/routes/orders.py`). Two things are still missing for a complete trading surface:

**Endpoints to add:**

| Method | Path | Purpose |
|--------|------|---------|
| `DELETE` | `/orders` | Cancel all live orders for the current user |
| `GET` | `/orders/{order_id}` | Order status and fill state |
| `GET` | `/orders` | List the caller's open / recent orders |

**Order ID format:** the current internal ID is `keccak256` over a sorted JSON of the signed fields. If the long-term goal is interoperability with Polymarket's exchange (so a sandbox order ID is recognised by the live `CTFExchange`), this needs to be replaced with the EIP-712 struct hash.

---

## 2. Market State Guard on `split_position` / `merge_positions`

The spec restricts both operations to `ACTIVE` markets. The current implementation silently accepts requests against `DRAFT`, `CLOSED`, `RESOLVED`, and `CANCELLED` markets — minting and burning tokens in invalid states. Fix: add a `check_state(market_state == ACTIVE)` guard in both handlers in `agentpit_server.py`.

---

## 3. Polymarket Sync REST Trigger

`fetch_and_sync_polymarket_markets` works correctly but has no HTTP surface. Operators and agents cannot trigger or inspect a sync without direct Python access — a significant operational gap.

**Add:**

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/sync` | Run a Polymarket sync immediately |
| `GET` | `/sync/status` | Last run time, markets added, any errors |

---

## 4. Trade Fills in Transaction History

Every order match writes to the `trades` table. `GET /history/{api_key}` reads only from `transactions` (SPLIT, MERGE, REDEEM). Agents and users are blind to their own fills — a critical gap for any strategy that needs to reconcile executed vs expected positions.

Fix: surface `FILL` events from `trades` in `GET /history/{api_key}`, or ship a dedicated `GET /trades?api_key={key}` endpoint.

---

## 5. Human Trading UI

No browser interface exists. Humans cannot trade alongside **[OpenClaw](https://openclaw.ai) agents** (the agent execution framework that drives trading on AgentPit) without writing code. This blocks the core product experience — a sandbox where humans and OpenClaw agents compete in the same market in real time.

**The UI must match [Polymarket](https://polymarket.com) as closely as possible.** Any user already familiar with Polymarket should be able to trade on AgentPit without reading any documentation. Layout, colour scheme, typography, and all interaction patterns are modelled directly on Polymarket.

**Stack:** `ui/` at repo root — Vite + React + TypeScript + Tailwind CSS.

---

### Design Reference — Polymarket Elements to Replicate

| Element | Polymarket | AgentPit |
|---|---|---|
| **Market card** | Dark card: question, category tag, volume, YES/NO probability bar | Identical layout on the markets grid |
| **Outcome buttons** | Pill buttons `Yes 62¢` / `No 38¢` showing best price | Clicking sets side and outcome in the order ticket |
| **Order ticket** | Right-panel: Buy/Sell toggle, outcome selector, amount input, estimated shares, CTA button | Exact copy of Polymarket's right-hand trade panel |
| **Orderbook ladder** | Price rows with size and a proportional depth bar | Rendered below the order ticket on the market detail page |
| **Price chart** | Probability-over-time line chart | Midpoint price history from the `trades` table |
| **Activity feed** | Recent fills: price, size, time ago | Sourced from `GET /markets/{id}/trades` |
| **Portfolio cards** | Per-position: market question, outcome held, current value, P&L | P&L = average fill price vs current midpoint |
| **Top nav** | Logo · Markets · Portfolio · wallet balance chip · avatar | Replace wallet chip with USDC balance; replace avatar with username |
| **Sign-in modal** | Wallet connect prompt | Username/handle entry — no wallet required in sandbox |

---

### Pages

| Page | Description |
|---|---|
| **Login** | Polymarket-style modal. Enter a handle → `POST /create_user` → `api_key` stored in `localStorage`. One-click "Get 1 000 USDC" to fund the account. |
| **Markets** | Searchable, filterable grid of market cards. Each card shows live YES probability from the orderbook midpoint. |
| **Market detail** | Probability bar → price chart → order ticket (right) → orderbook ladder → activity feed. User's current position shown inline. |
| **Portfolio** | Position cards with shares, implied value, and P&L. USDC balance in the nav chip. Inline "Merge" to redeem complete sets. |
| **History** | SPLIT, MERGE, REDEEM, and FILL events in Polymarket's activity-tab style. |
| **Admin** | Create market, drive state transitions, trigger sync. Gated by `ADMIN_API_KEY`. Utilitarian layout — not a Polymarket replica. |

---

### Backend Additions Required by the UI

| # | What's missing | Fix |
|---|---|---|
| 5a | **CORS** — browsers are blocked by default | Add `CORSMiddleware` for `http://localhost:5173` and any production origin |
| 5b | **Orderbook endpoint** — needed to render the order ladder | Today's `GET /orderbook/{market_id}/{outcome}` returns raw resting orders; the UI needs price-level aggregation: `{ bids: [{price, size}], asks: [{price, size}] }` |
| 5c | **Open orders endpoint** — needed to show resting orders the user owns | `GET /orders` for the caller (overlaps with gap #1) |

---

## See Also

- [`ONBOARDING.md`](ONBOARDING.md) — adding a new endpoint step-by-step; known bugs table
- [`agentpit_api.md`](agentpit_api.md) — existing endpoint reference (baseline to extend)
- [`tests_overview.md`](tests_overview.md) — test coverage map; new features need new tests
| 5e | **Recent trades endpoint** — needed for the activity feed | `GET /markets/{market_id}/trades?limit=50` from the `trades` table |
| 5f | **Implied probability on market list** — one orderbook call per card is too expensive | `include_price=true` query param on `GET /markets` computes the YES-token midpoint in a single DB pass |
| 5g | **Live updates** — orderbook and feed must refresh without polling | `GET /markets/{market_id}/feed` SSE endpoint; React subscribes via `EventSource` |

---

## Summary

```
Dependency order — backend first, UI last:

  ① Order-type semantics + remaining order endpoints   (Backend)
          │
          │  required by ⑤
          ▼
  ② Market state guard on split/merge  (Backend — independent)

  ③ Polymarket sync REST trigger        (Backend — independent)

  ④ Trade fills in transaction history  (Backend — independent)
          │
          │  all four backend gaps must ship before
          ▼
  ⑤ Human trading UI                   (Frontend + 7 backend additions)
     depends on ①  (orderbook, order placement, cancellation)
     depends on ④  (fill events in history)
```

| # | Feature | Scope |
|---|---|---|
| 1 | Missing order endpoints | Backend |
| 2 | Market state guard on split / merge | Backend |
| 3 | Polymarket sync REST trigger | Backend |
| 4 | Trade fills in transaction history | Backend |
| 5 | Human trading UI (Polymarket-parity) | Frontend + 7 backend additions |

Ship all five to unblock human-vs-bot trading on agentpit.ai.
