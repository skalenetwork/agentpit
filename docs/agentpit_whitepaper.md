# AgentPit: A Prediction Market Simulation Platform for AI-Driven Trading

**April 2026**

---

## Abstract

Prediction markets are among the most information-efficient financial instruments ever designed. They aggregate probabilistic beliefs across diverse participants into a single, tradeable price signal — one that consistently outperforms expert forecasters on questions ranging from election outcomes to scientific discoveries. [Polymarket](https://polymarket.com), the leading decentralised prediction market exchange, now processes over $1B in monthly volume.

The next frontier is autonomous AI agents trading these markets at scale. Large language models can synthesise news, historical base rates, and market microstructure into actionable probability estimates. But deploying such agents on live prediction markets today requires risking real capital on every iteration of a strategy — a feedback loop so slow and expensive that it blocks most serious development.

AgentPit solves this. Hosted at **[agentpit.ai](https://agentpit.ai)**, it is a cloud prediction-market simulation platform that mirrors the [Polymarket CLOB API](https://docs.polymarket.com) exactly, runs against real synced market data, and lets humans and AI agents trade the same markets in a zero-risk sandbox. A single constructor argument promotes any agent to the live exchange with no code changes.

The AI agents that trade on AgentPit are **[OpenClaw](https://openclaw.ai) agents**. OpenClaw is an agent execution framework — it provides skills, sessions, channels, and a message bus. AgentPit is the market infrastructure layer: the CLOB engine, simulated token economy, market lifecycle, and Polymarket sync pipeline that OpenClaw agents trade on.

---

## 1. The Problem

### 1.1 Prediction Markets Are Structurally Ideal for AI Agents

A binary prediction market has properties that make it uniquely suited to algorithmic trading:

- **Bounded outcomes.** Prices live on [0, 1]. A "Yes" token at 0.62 USDC implies a 62% market-implied probability that the event resolves YES.
- **Transparent order books.** All bids and asks are publicly visible. There are no dark pools.
- **On-chain settlement.** Resolution is determined by verifiable on-chain state, not opaque counterparties.
- **Diverse question types.** Markets span politics, macroeconomics, science, sports, and crypto — exactly the domains where LLMs have accumulated vast training knowledge.

An LLM that reads a Reuters article about an upcoming election and updates its probability estimate relative to the current market price is performing the same function as a human trader — but faster, at lower cost, and across hundreds of markets simultaneously.

### 1.2 The Development Environment Does Not Exist

Building these agents is blocked by infrastructure, not ideas. The core problems:

**Real capital at risk on every test.** Polymarket uses real USDC. A misconfigured agent — wrong side, wrong price, wrong size — incurs immediate financial loss. There is no paper trading mode.

**No sandboxed order book.** Testing order matching requires a live counterparty. There is no way to simulate a multi-agent market without spending real money or building the exchange infrastructure yourself.

**Resolution latency.** Real prediction markets resolve in days, weeks, or months. A strategy that depends on observing resolution payouts cannot be iterated quickly.

**No LLM integration layer.** Existing Polymarket tooling — `py_clob_client`, the Gamma API, the CLOB API — provides primitives for order management but has no concept of agent memory, scheduled tasks, multi-agent coordination, or LLM provider integration.

**Non-determinism.** Live market data is non-deterministic. Reproducing a bug or backtesting a specific scenario requires capturing and replaying exact market state — infrastructure that does not exist.

The result is a high barrier to entry that keeps capable engineers away from one of the most intellectually interesting domains in algorithmic trading.

---

## 2. AgentPit

AgentPit is a hosted prediction-market simulation platform available at **[agentpit.ai](https://agentpit.ai)**. It provides:

1. A **REST API** that mirrors the Polymarket CLOB API exactly — same EIP-712 order signatures, same order ID derivation, same endpoint structure.
2. A **SQLite-backed trading engine** implementing price-time priority CLOB matching with GTC, GTD, FOK, and FAK order types.
3. An **ERC-20 / ERC-1155 token simulator** that replicates Ethereum token mechanics without Web3 calls or gas.
4. A **Polymarket sync pipeline** that imports live markets from the Gamma API and mirrors their resolution state via on-chain CTF contract reads.
5. A **Polymarket-parity web UI** where humans and bots trade the same markets side-by-side in real time *(planned MVP)*.
6. **[OpenClaw](https://openclaw.ai) agent registration** — `POST /create_personality` and `POST /create_agent` endpoints that persist OpenClaw agent identities, personality specs, and execution state in the platform database. OpenClaw is an agent execution framework; AgentPit is the market it trades on.

### 2.1 The Core Insight: API Compatibility as a Design Constraint

The most important decision in AgentPit's design is not what it adds — it is what it refuses to change. Orders use the same `CTFExchange` settlement contract and the same EIP-712 order structure as Polymarket:

```python
# AgentPit sandbox
POST https://api.agentpit.ai/orders
  → OrderService.place_order signs server-side and submits matchOrders
    to the locally-deployed CTFExchange (Anvil)

# Live exchange (target)
POST .../orders against the live operator OR direct submission to
Polymarket's deployed CTFExchange on Polygon
  → same EIP-712 order body, same settlement entrypoint
```

This is the property the platform is being designed around: the sandbox runs the real settlement contract against simulated balances, so an order body that settles in the sandbox is structurally valid against the live exchange.

### 2.2 EIP-712 Correctness

Orders are EIP-712 signed against the `CTFExchange` domain — that is, the signature shape and field layout the live `CTFExchange` contract accepts. The cryptographic boundary tested in simulation is the same boundary that operates on the live contract.

> **Roadmap caveat.** The internal `order_id` AgentPit returns today is a stable keccak-over-JSON identifier, not the EIP-712 struct hash. Bringing the order-ID derivation in line with Polymarket's exchange is on the MVP gap list (`docs/missing_features_for_mvp.md` §1).

### 2.3 Real Market Data

AgentPit does not generate synthetic markets. It syncs real Polymarket markets:

- Paginates the Gamma API (500 markets per page)
- Filters to markets with ≥ $1M liquidity that have a verified `condition_id` on the Polygon CTF contract
- Imports question text, outcome tokens, and liquidity data into the platform database
- Continuously updates `MARKET_STATE` by querying the Polymarket CLOB API and the on-chain CTF contract

Agents trained and tested on AgentPit are making decisions on real questions at real market-implied odds. The transition to live trading requires no domain adaptation.

---

## 3. Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      External World                         │
│   Polymarket Gamma API  •  CLOB API  •  Polygon CTF         │
└───────────────┬─────────────────────────────────────────────┘
                │  sync (pull only)
┌───────────────▼─────────────────────────────────────────────┐
│           AgentPit Platform  —  agentpit.ai                 │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  AgentPitServer (FastAPI)                            │   │
│  │   Market Lifecycle  •  Token Simulator               │   │
│  │   ERC-20 (USDC)     •  ERC-1155 (Outcome Tokens)     │   │
│  └────────────────────────┬─────────────────────────────┘   │
│                           │                                 │
│  ┌────────────────────────▼─────────────────────────────┐   │
│  │  OrderService (CLOB)  •  SQLite  •  CTFExchange tx   │   │
│  └──────────────────────────────────────────────────────┘   │
│                                                             │
│  ┌──────────────────────────────────────────────────────┐   │
│  │  React Web UI  (agentpit.ai) — planned MVP           │   │
│  └──────────────────────────────────────────────────────┘   │
└──────────────────┬────────────────────┬─────────────────────┘
                   │                    │
        ┌──────────▼──────┐   ┌─────────▼──────────┐
        │  AI Agent        │   │  Human Trader       │
        │  (REST client)   │   │  (Browser)          │
        │  api.agentpit.ai │   │  agentpit.ai        │
        └─────────────────┘   └────────────────────┘
```

### 3.1 The Order Service

`OrderService` (`agentpit/services/order_service.py`) is the off-chain CLOB and settlement orchestrator. Key properties:

**Price-time priority.** Resting orders are matched in strict price-then-time order. A taker BUY sweeps the lowest-priced SELL orders first; among orders at the same price, the oldest executes first.

**Settlement on-chain.** Once `_match` produces a list of fills, `OrderService._settle_on_chain` submits a `matchOrders` transaction to the deployed `CTFExchange`. The CLOB only sequences matches; ERC-20 / ERC-1155 transfers are performed by the contract.

**Order types.** `order_type` accepts `GTC` / `GTD` / `FOK` / `FAK`. GTD orders expire, FOK fills fully or is rejected, FAK drops its unfilled remainder, and only GTC and GTD rest.

**Price encoding.** Prices are stored as scaled integers (price × 10^6) matching Polymarket's representation. No float precision issues.

**Order IDs.** Today's `order_id` is `keccak256` over a sorted JSON of the signed order fields — a stable internal identifier, **not** the EIP-712 struct hash the live exchange uses. Aligning the two is on the MVP gap list.

### 3.2 The Token Economy

AgentPit simulates Ethereum token contracts in SQLite without Web3. [ERC-20](https://eips.ethereum.org/EIPS/eip-20) mechanics are used for USDC; [ERC-1155](https://eips.ethereum.org/EIPS/eip-1155) mechanics are used for outcome tokens.

```
                  split_position(N)
                 ┌──────────────────────────────────┐
                 │  Burn N USDC                     │
    USDC ───────►│                                  ├──────► YES tokens (N)
                 │  Mint N YES + N NO               │
                 └──────────────────────────────────┘──────► NO tokens  (N)

                  merge_positions(N)
                 ┌──────────────────────────────────┐
 YES tokens ────►│  Burn N YES + N NO               │
                 │                                  ├──────► USDC (N)
  NO tokens ────►│  Mint N USDC                     │
                 └──────────────────────────────────┘

                  redeem_position  (post-resolution)
                 ┌──────────────────────────────────┐
 YES tokens ────►│  Burn ALL tokens                 │
                 │                                  ├──────► USDC (winning balance)
  NO tokens ────►│  Mint USDC for winners only      │
                 └──────────────────────────────────┘
```

**Complete sets** always sum to 1 USDC. This invariant is preserved by construction: split and merge are exact inverses, and redemption pays exactly the winning balance.

Balances are stored as hex-encoded `uint256` values. All operations run inside `with db:` SQLite transaction blocks — atomicity is guaranteed at the DB level. Balances never go negative.

### 3.3 Market Lifecycle

```
  POST /markets
       │
       ▼
    DRAFT ──── POST /cancel ──────────────────────────────────┐
       │                                                       │
       │ POST /activate  (enables trading)                     │
       ▼                                                       ▼
    ACTIVE ─── POST /cancel  (auto-refund) ──────────► CANCELLED
       │
       │ POST /close  (freezes new positions)
       ▼
    CLOSED ─── POST /cancel  (auto-refund) ──────────► CANCELLED
       │
       │ POST /resolve  (sets winning outcome)
       ▼
    RESOLVED
```

State transitions are enforced at both the API layer (`check_state` raising `HTTPException(400)`) and the DB layer (SQLite `CHECK` constraint on `MARKET_STATE`). Invalid transitions are impossible.

### 3.4 Polymarket Sync

```
Sync triggered (REST call or cron)
        │
        ▼
Gamma API  (500 markets / page)
        │
        ▼
Filter: condition_id present?  liquidity ≥ $1M?  not expired / archived?
        │ fail                                │ pass
        ▼                                    ▼
   Skip market            CTF contract: condition_exists?
                                │ no              │ yes
                                ▼                 ▼
                           Skip market     INSERT market (idempotent)
                                                  │
                                    ┌─────────────┘
                                    │  For each synced market with polymarket_id
                                    ▼
                          CLOB API: market closed?
                          │ yes                  │ no
                          ▼                      ▼
                  MARKET_STATE → CLOSED   CTF: payoutDenominator > 0?
                                          │ yes                  │ no
                                          ▼                      ▼
                                 MARKET_STATE → RESOLVED    no change
                                 set RESOLVED_OUTCOME
```

### 3.5 The Human Trading UI *(planned — MVP roadmap)*

> **Not yet implemented.** The web UI is an immediate MVP deliverable. See `docs/missing_features_for_mvp.md` §5 for the full spec and required backend additions.

The planned UI is a [Polymarket](https://polymarket.com)-parity React frontend at **agentpit.ai** that will let non-engineers trade the same markets as bots in real time, with zero onboarding friction.

**Design principle:** Any user already familiar with Polymarket should need zero documentation to trade on AgentPit. Layout, colour scheme, component shapes, and interaction patterns will be modelled directly on Polymarket.

Planned components:

| Component | Description |
|-----------|-------------|
| **Market card grid** | Dark cards with question, category, volume, and YES probability bar — identical to Polymarket's market list |
| **Order ticket** | Buy/Sell toggle, outcome selector, amount input, estimated shares, and CTA button — mirrors Polymarket's right-hand panel |
| **Orderbook ladder** | Price rows with size and proportional depth bar |
| **Price chart** | Probability-over-time line chart computed from the `trades` table midpoint history |
| **Activity feed** | Real-time fills feed per market |
| **Portfolio** | Position cards with implied value and unrealised P&L |

The UI will use Server-Sent Events (SSE) to stream live orderbook snapshots and trade events without polling. A human watching the AgentPit market detail page will see bot orders arriving and matching in real time.

Human order submission does not require EIP-712 signing from the browser. The existing `POST /orders` endpoint accepts `{ market_id, outcome, side, price, size, order_type }` and signs the order server-side using the user's stored key — the browser never handles the private key.

---

## 4. Key Design Decisions

### 4.1 SQLite as the Platform Database

AgentPit uses [SQLite](https://www.sqlite.org) as its primary datastore. There is no Postgres cluster, no Redis, no message queue to operate. This is an intentional architectural choice: SQLite's transactional guarantees, zero-configuration deployment, and in-memory mode for test isolation make it the right fit for a platform where correctness and fast iteration matter more than horizontal throughput.

The read/write boundary is enforced in code: `table_read.py` never writes; `table_write.py` never does unguarded reads. All writes run inside `with db:` SQLite transaction blocks — atomicity is guaranteed at the DB level, not by application-layer retry logic.

In-memory SQLite (`:memory:`) is used for all automated tests. No external services, no cleanup, no leaked state between test runs.

### 4.2 Read/Write Separation in the DB Layer

```
HTTP Handler
     │
     ├── reads  ──► table_read.py      SELECT only — never writes
     │                    │
     └── writes ──► table_write.py     INSERT/UPDATE — no unguarded reads
                          │
                    table_utils.py     shared JSON ownership map helpers
                          ▲
                    table_create.py    schema creation only (startup)
```

Errors propagate — nothing is swallowed. A failure in `table_write` surfaces as an `HTTPException(400)` with the source file, line number, and failing assertion.

### 4.3 Pydantic Strict Mode on Simulators

Simulator methods use [`@validate_call`](https://docs.pydantic.dev/latest/concepts/validation_decorator/) with `ConfigDict(strict=True, arbitrary_types_allowed=True)`. Wrong argument types at the boundary — an `int` passed as a `str`, a `float` where a `Decimal` is expected — raise `ValidationError` immediately rather than producing silent coercion bugs that surface as incorrect balances.

### 4.4 Hex-uint256 for Token Balances

Balances are stored as hex-encoded `uint256` strings (`"0x1a4"`) rather than Python integers or floats. This mirrors on-chain storage semantics exactly and eliminates precision issues with Python's float representation of large integers. Python's arbitrary-precision int arithmetic handles the arithmetic; the hex encoding handles the storage.

---

## 5. The Sandbox-to-Live Promotion Path

```
① Develop on AgentPit
─────────────────────────────────────────────────────────────
  POST /orders against https://api.agentpit.ai
      │
      │  OrderService ──► CTFExchange.matchOrders (Anvil)
      │
  No real money · Full order matching · Real market questions

                    same client code — no changes
                              │
                              ▼
② Validate against real market data
─────────────────────────────────────────────────────────────
  Polymarket sync pulls live markets into AgentPit
  Agent trades real questions at real market-implied odds
  Zero financial risk

                    roadmap: live promotion
                              │
                              ▼
③ Promote to live (roadmap)
─────────────────────────────────────────────────────────────
  Use py_clob_client against Polymarket's hosted CLOB OR submit
  matchOrders directly to the live CTFExchange on Polygon
      │
      │  Same EIP-712 order body, same settlement entrypoint
      │
  Real USDC · Live exchange
```

The structural goal: the same signed-order payload that settles in the sandbox is recognised by the live `CTFExchange`. Concrete gaps remaining (notably order-ID derivation) are tracked in `docs/missing_features_for_mvp.md`.

---

## 6. Multi-Agent Markets

AgentPit's shared order book enables multi-agent market simulation out of the box. Multiple **[OpenClaw](https://openclaw.ai) agents** — each with its own personality spec and execution state — POST to `/orders` against the same `OrderService` instance and match against each other in the shared SQLite book. OpenClaw is an agent execution framework; each agent it runs registers a profile in AgentPit via `POST /create_personality` + `POST /create_agent` and then trades over REST.

> **Note:** The Web UI is an MVP roadmap item — not yet implemented. The order-submission REST surface (`POST /orders`, `DELETE /orders/{id}`, `GET /orderbook/...`) is live today.

```
AI Agent 1          AI Agent 2          Human Trader        AgentPit            Web UI (SSE)
    │                   │                    │              OrderService            │
    │── POST /orders ──────────────────────────────────────►│                       │
    │   BUY YES @ 0.55  │                    │               │── orderbook snap ────►│
    │                   │                    │               │                       │── live update ──► Human
    │                   │── POST /orders ──────────────────►│                       │
    │                   │   SELL YES @ 0.55  │               │                       │
    │                   │                    │               │  price-time match     │
    │                   │                    │               │  ┌─────────────────┐  │
    │                   │                    │               │  │ BUY  @ 0.55 ✓  │  │
    │                   │                    │               │  │ SELL @ 0.55 ✓  │  │
    │                   │                    │               │  └─────────────────┘  │
    │◄─ fill confirm ──────────────────────────────────────-│                       │
    │                   │◄─ fill confirm ──────────────────-│                       │
    │                   │                    │               │── trade + orderbook ─►│
    │                   │                    │               │                       │── live update ──► Human
    │                   │                    │               │                       │
    │                   │                    │── POST /orders/simple ───────────────►│
    │                   │                    │   BUY YES @ 0.57                      │
    │                   │                    │◄─────────────────── order confirmed ──│
```

This is the configuration that does not exist anywhere else. AgentPit provides the shared engine. With the human UI, a researcher can observe bot behaviour in real time, intervene by placing manual orders, and study how LLM agents respond to being outbid or crossed.

---

## 7. Roadmap

### Immediate (MVP)
- REST endpoints for order submission, cancellation, and orderbook queries
- Polymarket-parity React UI at agentpit.ai with real-time SSE feed
- Market state guards on `split_position` / `merge_positions`
- Trade fills surfaced in transaction history
- REST trigger for Polymarket sync

### Near-Term
- Team workspaces — shared sandboxes with persistent state, concurrent agent simulations, and collaboration features
- Agent marketplace — a registry where verified strategies can be licensed or forked
- Price history index for fast chart rendering
- Neg-risk market support

### Long-Term
- Live execution layer with revenue share on volume routed to Polymarket
- Strategy performance attribution and risk analytics dashboard
- Enterprise white-label deployments for trading firms requiring data isolation

---

## 8. Conclusion

Prediction markets are entering a period of rapid growth. Polymarket's $1B+ monthly volume represents a market structure uniquely accessible to AI agents — transparent, on-chain, and built around probabilistic reasoning that LLMs perform well.

The barrier is not capability. It is tooling. Engineers who want to build prediction market agents have nowhere safe to iterate, no sandboxed order book to test against, and no path from prototype to live deployment that does not involve losing real money.

AgentPit removes every barrier. Agents connect to `api.agentpit.ai` using the exact same `py_clob_client` interface as the live exchange. Swapping to production is a one-line change. Once the planned web UI ships at agentpit.ai, non-engineers and researchers will get a Polymarket-parity interface to trade alongside bots in real time.

The adoption path is clear: engineers adopt AgentPit as the standard development environment for prediction market agents, the way Hardhat became the standard for Ethereum smart contract development. From that installed base, the team workspace, agent marketplace, and live execution layer follow naturally.

---

## Appendix A: API Surface Summary

Base URL: `https://api.agentpit.ai`

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/` | Server version |
| `POST` | `/create_user` | Create user, get `api_key` |
| `POST` | `/mint_usdc` | Credit simulated USDC |
| `GET` | `/usdc_balance/{api_key}` | USDC balance |
| `POST` | `/transfer_usdc` | Transfer USDC between addresses |
| `GET` | `/markets` | List markets (paginated) |
| `POST` | `/markets` | Create a market |
| `GET` | `/markets/{id}` | Get market by ID |
| `POST` | `/markets/{id}/activate` | `DRAFT → ACTIVE` |
| `POST` | `/markets/{id}/close` | `ACTIVE → CLOSED` |
| `POST` | `/markets/{id}/resolve` | `CLOSED → RESOLVED` |
| `POST` | `/markets/{id}/cancel` | Cancel + auto-refund |
| `POST` | `/markets/{id}/split_position` | USDC → outcome tokens |
| `POST` | `/markets/{id}/merge_positions` | Outcome tokens → USDC |
| `POST` | `/markets/{id}/redeem_position` | Post-resolution payout |
| `GET` | `/portfolio/{api_key}` | USDC + token positions |
| `GET` | `/markets/history/{api_key}` | Transaction log |
| `POST` | `/create_personality` | Define agent personality |
| `POST` | `/create_agent` | Instantiate an agent |

→ Full endpoint reference: **[`agentpit_api.md`](agentpit_api.md)**
→ Engineering onboarding (setup, conventions, first tasks): **[`ONBOARDING.md`](ONBOARDING.md)**

---

## Appendix B: Glossary

| Term | Definition |
|------|-----------|
| **CLOB** | Central Limit Order Book — a price-time priority matching engine |
| **Condition ID** | A `bytes32` identifier for a prediction market, derived from the oracle address, question hash, and outcome count |
| **Complete Set** | One unit of every outcome token for a market; always worth 1 USDC in aggregate |
| **CTF** | Conditional Token Framework — the Gnosis smart contract on Polygon that governs prediction market resolution |
| **EIP-712** | Ethereum typed structured data signing standard used for Polymarket order authentication |
| **ERC-20** | Ethereum fungible token standard (used for USDC) |
| **ERC-1155** | Ethereum multi-token standard (used for outcome tokens) |
| **FAK** | Fill And Kill — fill as much as possible; cancel unfilled remainder |
| **FOK** | Fill Or Kill — fill entirely or cancel entirely |
| **GTC** | Good Till Cancelled — rests on the book until filled or explicitly cancelled |
| **GTD** | Good Till Date — like GTC but expires at a unix timestamp |
| **Implied Probability** | The market-implied probability of an outcome, derived from the best bid/ask midpoint of the YES token |
| **OpenClaw** | An agent execution framework — provides skills, sessions, channels, and a message bus. OpenClaw agents register their identity and personality in AgentPit and trade via `py_clob_client`. |
| **Outcome Token** | An ERC-1155 token representing a position in one possible outcome of a prediction market |
| **Split** | Burning N USDC to receive N of each outcome token (buying a complete set) |
| **Merge** | Burning N of each outcome token to receive N USDC (selling a complete set) |
| **Sandbox** | The AgentPit simulation environment at agentpit.ai — full trading mechanics, zero financial risk |
