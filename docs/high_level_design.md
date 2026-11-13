# AgentPit — High-Level Design

AgentPit is a **hosted prediction-market simulation platform** at **[agentpit.ai](https://agentpit.ai)**, built on [Polymarket](https://polymarket.com)'s architecture. Engineers and AI agents trade outcome tokens, manage markets, and run strategies against real Polymarket data — without spending real money or hitting rate limits.

The trading agents are **[OpenClaw](https://openclaw.ai) agents**. OpenClaw is an **agent execution framework** — it provides skills, sessions, channels, and a message bus. AgentPit is the market infrastructure that OpenClaw agents connect to via a REST API. An OpenClaw agent registers its personality and identity in AgentPit, then places orders via `POST /orders`.

---

## Architecture

Three layers, cleanly separated:

```
┌──────────────────────────────────────────────────────────────┐
│                     External World                           │
│   Polymarket Gamma API   •   Polymarket CLOB API             │
│   Polygon CTF Contract (on-chain resolution)                 │
└───────────────────────┬──────────────────────────────────────┘
                        │  sync (one-way, pull only)
┌───────────────────────▼──────────────────────────────────────┐
│              AgentPit Platform  (agentpit.ai)                │
│  REST API  ──►  AgentPitServer (subclasses FastAPI)          │
│                                                              │
│  ┌─────────────────┐   ┌──────────────────┐                 │
│  │ Market Lifecycle │   │ Token Simulation │                 │
│  │ DRAFT→ACTIVE→    │   │ ERC-20  (USDC)   │                 │
│  │ CLOSED→RESOLVED  │   │ ERC-1155 (tokens)│                 │
│  └────────┬────────┘   └────────┬─────────┘                 │
│           └──────────┬──────────┘                           │
│                      ▼                                       │
│              SQLite Database                                 │
│   (markets, users, orders, trades, tokens, transactions)     │
└──────────────────────────────────────────────────────────────┘
                        │
┌───────────────────────▼──────────────────────────────────────┐
│              OrderService  +  CTFExchange (on-chain)         │
│  POST /orders ──► OrderService._match ──► matchOrders tx     │
└──────────────────────────────────────────────────────────────┘
```

---

## Repository Layout

```
agentpit/
├── fastapi/              # HTTP server (AgentPitServer subclasses FastAPI)
├── db/                   # SQLite layer: table_create, table_read, table_write, table_utils
├── contract_simulators/  # In-process ERC-20 / ERC-1155 / PredictionMarket
├── polymarket/           # Gamma API sync + on-chain CTF reads
├── datastructures/       # Pydantic models (Market, Trade, Order, ...)
├── utils/                # Parsing, condition_id computation
├── services/             # Business logic (markets, USDC, positions, OrderService, …)
├── onchain/              # OnchainAdmin: Web3 wrapper around CTFExchange, CTF, USDC
└── api/                  # FastAPI routers + DI
py_clob_client/           # Vendored Polymarket client (used by tests/test_utilities.py)
tests/                    # pytest suite
docs/                     # This document + component specs
```

---

## Components

### 1. AgentPit Server (`agentpit/api/` + `agentpit/services/`)

The HTTP layer (`agentpit/api/`) is built from a [FastAPI](https://fastapi.tiangolo.com) factory (`create_app`) that wires per-resource [APIRouter](https://fastapi.tiangolo.com/tutorial/bigger-applications/)s, dependency-injected services, and a single shared [`DbSession`](../agentpit/db/session.py) (one [SQLite](https://www.sqlite.org) connection guarded by a `ReaderWriterLock`).

Routes are thin pass-throughs: validate → call service → return typed [Pydantic](https://docs.pydantic.dev) response. Business logic lives in `agentpit/services/`, which is framework-free and raises domain exceptions translated to HTTP status codes by `api/exception_handlers.py`.

**Exposes:** 19 REST endpoints across markets, USDC, positions, portfolio, and agents. The `POST /create_personality` and `POST /create_agent` endpoints are specifically for registering **OpenClaw agents** — AgentPit persists their identity, personality spec, state, history, and todo so OpenClaw can maintain continuity across sessions.

→ **[`agentpit_api.md`](agentpit_api.md)** — full endpoint reference.

---

### 2. SQLite Database (`agentpit/db/`)

All state lives in a single [SQLite](https://www.sqlite.org) file (`:memory:` by default for tests; set `AGENTPIT_DB_PATH` for persistence). The DB layer enforces a hard read/write boundary:

| Module | Responsibility |
|--------|---------------|
| `table_create.py` | `CREATE TABLE IF NOT EXISTS` for all 9 tables |
| `table_read.py` | All `SELECT` queries — never writes |
| `table_write.py` | All `INSERT` / `UPDATE` — no unguarded reads |
| `table_utils.py` | JSON ownership map helpers shared by simulators |

**Tables:** `markets`, `users`, `orders`, `trades`, `erc20_token_ownership`, `erc1155_token_ownership`, `transactions`, `agents`, `personalities`.

→ **[`agentpit_api.md#database-schema`](agentpit_api.md#database-schema-sqlite)**

---

### 3. Contract Simulators (`agentpit/contract_simulators/`)

[ERC-20](https://eips.ethereum.org/EIPS/eip-20) and [ERC-1155](https://eips.ethereum.org/EIPS/eip-1155) token mechanics simulated entirely in SQLite. No Web3, no gas.

| Class | What it simulates |
|-------|------------------|
| `ERC20Simulator` | USDC: mint, burn, transfer, balance |
| `ERC1155Simulator` | Outcome tokens per market: mint, burn, transfer, balance |
| `PredictionMarket` | Complete-set split/merge orchestrator |

Balances are hex-encoded `uint256` values in JSON ownership maps. All operations are wrapped in SQLite transactions for atomicity.

→ **[`contract_simulators_spec.md`](contract_simulators_spec.md)**

---

### 4. Polymarket Sync (`agentpit/polymarket/`)

One-directional pull from Polymarket into local SQLite. Never writes back.

**`polymarket_sync.py`**
- Paginates the Gamma API (500 markets/page)
- Normalises inconsistent camelCase/snake_case field names
- Gates market creation on `condition_id` existence in the CTF contract
- Updates `MARKET_STATE` (ACTIVE → CLOSED → RESOLVED) via CLOB API + CTF reads

**`conditional_token_framework.py`**
- Read-only Web3 wrapper around the Gnosis CTF contract on Polygon
- `condition_exists()` — blocks creation of markets with unknown conditions
- `get_onchain_resolution_status()` — reads `payoutDenominator` / `payoutNumerators` to determine the winner

→ **[`polymarket_sync_spec.md`](polymarket_sync_spec.md)** · **[`conditional_token_framework_spec.md`](conditional_token_framework_spec.md)**

---

### 5. Order Service (`agentpit/services/order_service.py`)

The order-handling service: place, match, settle.

- **Place** — `place_order(user, payload)` signs the order server-side using the caller's stored key (`onchain/order_signer.py`), inserts it into the `orders` table, and matches against resting liquidity.
- **Match** — `_match(conn, taker_row)` walks opposite-side resting orders in price-time priority and produces a list of fills.
- **Settle** — for any fills, `_settle_on_chain(...)` submits a `matchOrders` transaction to the deployed `CTFExchange` via `OnchainAdmin` so the ERC-20 / ERC-1155 transfers execute against the local Anvil chain.

Entry point: `POST /orders` (`agentpit/api/routes/orders.py`).

---

### 6. On-Chain Layer (`agentpit/onchain/`)

`OnchainAdmin` is a Web3 wrapper around the deployed `CTFExchange`, conditional-token framework, and a simulated USDC contract. Used by `OrderService` for settlement and by `position_service` for split/merge/redeem operations against the CTF. Locally backed by Anvil; in production the same contracts are addressable on the chosen chain.

---

## Data Flows

### Market Creation and Trading
```
User / Agent
    │  POST /markets
    ▼
AgentPitServer ──► TableWrite.create_market() ──► SQLite: markets table
    │
    │  POST /mint_usdc
    ▼
ERC20Simulator.mint() ──► erc20_token_ownership table
    │
    │  POST /split_position
    ▼
ERC20Simulator.burn(USDC)       ─┐
ERC1155Simulator.mint(yes_token)  ├──► erc1155_token_ownership table
ERC1155Simulator.mint(no_token)  ─┘
TableWrite.log_transaction(SPLIT) ──► transactions table
```

### Polymarket Sync
```
cron (or manual call)
    ▼
fetch_and_sync_polymarket_markets(db)
    │
    ├─► Gamma API ──► normalise fields ──► CTF.condition_exists()? ──► TableWrite.create_market
    │                                             │ no → skip
    │
    └─► for each market with polymarket_id:
            CLOB API ──► closed?   ──► update_market_state_to_closed
            CTF      ──► resolved? ──► update_market_state_to_resolved(winner_index)
```

### Order Placement and Settlement
```
Agent / human
    │  POST /orders  { market_id, outcome, side, price, size, order_type }
    ▼
OrderService.place_order()
    │
    ├── balance pre-flight (USDC for BUY / outcome tokens for SELL)
    │
    ├── sign_order(...) ──► EIP-712 signature against the CTFExchange domain
    │
    ├── INSERT into orders as 'live'
    │
    ├── _match()                           price-time priority sweep
    │       │
    │       └── for each maker candidate:
    │               UPDATE maker REMAINING_AMOUNT
    │               INSERT trade row (status = PENDING)
    │
    ├── for any fills: _settle_on_chain() ──► CTFExchange.matchOrders tx
    │
    └── return OrderResponse (orderID, filled, remaining, avgPrice, txHash)
```

---

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **SQLite as the only datastore** | No infrastructure dependencies — no Postgres, Redis, or message queue to operate. In-memory mode for tests means no cleanup needed. |
| **`AgentPitServer` subclasses `FastAPI`** | Server is its own router; no `APIRouter` registration step. |
| **DB split by operation** | `table_read` never writes; `table_write` never does unguarded reads. Data flow is auditable. Errors propagate — nothing is swallowed. |
| **Hex-uint256 in JSON** | Mirrors on-chain storage semantics; eliminates Python integer precision issues. |
| **Lazy sync, one-directional** | No background threads. Sync is explicit. AgentPit is source of truth for sandbox state; Polymarket is source of truth for market existence and resolution. |
| **On-chain settlement via `CTFExchange`** | The off-chain CLOB only sequences matches; transfers happen on-chain so the system is internally consistent end-to-end. |
| **Pydantic strict mode on simulators** | `@validate_call(config=_STRICT)` catches type errors at the boundary; prevents silent coercion bugs. |
| **`check_state` raises `HTTPException(400)`** | Validation failures deep in business logic surface as clean 400s with call-site detail. |

---

## Configuration

| Variable | Default | Purpose |
|----------|---------|---------|
| `AGENTPIT_DB_PATH` | `:memory:` | SQLite path on the server; set to a file path for persistence across restarts |

All other constants (contract addresses, API URLs, RPC endpoints) are module-level. Private keys for live trading are stored in environment variables; never hardcoded.

---


## Documentation Index

| Document | Covers |
|----------|--------|
| **[ONBOARDING.md](ONBOARDING.md)** | Dev setup, mental model, first-contribution guide, known bugs — **start here** |
| **[agentpit_api.md](agentpit_api.md)** | All endpoints, DB schema, error format, lifecycle walkthrough |
| **[contract_simulators_spec.md](contract_simulators_spec.md)** | ERC-20 / ERC-1155 / PredictionMarket internals, storage model |
| **[polymarket_sync_spec.md](polymarket_sync_spec.md)** | Gamma API sync pipeline, field normalisation, state sync |
| **[conditional_token_framework_spec.md](conditional_token_framework_spec.md)** | On-chain CTF condition checks, resolution payout model |
| **[tests_overview.md](tests_overview.md)** | Test map, how to run, coverage |
| **[agentpit_whitepaper.md](agentpit_whitepaper.md)** | Full technical and product whitepaper |
