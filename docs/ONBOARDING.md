# Engineering Onboarding

Welcome to AgentPit. This doc gets you from zero to first pull request in under an hour.

---

## Mental Model (read this first)

AgentPit is a **Polymarket sandbox**. Engineers and AI agents trade real prediction market questions using the exact same code they would use on the live Polymarket exchange — but with simulated USDC and no financial risk.

Three things make this work:

```
AgentPitServer   ──  FastAPI REST server (markets, USDC, positions, orders, agents)
OrderService     ──  SQLite CLOB (price-time priority) + on-chain settlement via
                     CTFExchange.matchOrders
OnchainAdmin     ──  Operator-side Web3 wrapper around the deployed CTFExchange, CTF,
                     and simulated USDC contracts (Anvil locally)
```

**Agents are OpenClaw agents.** [OpenClaw](https://openclaw.ai) is an **agent execution framework** that drives trading on AgentPit. OpenClaw provides the runtime — skills, sessions, channels, and a message bus — while AgentPit provides the market infrastructure. An OpenClaw agent is registered in AgentPit via `POST /create_personality` + `POST /create_agent`, then places orders via `POST /orders`. The agent's identity (`agent_id`, personality spec, state, history, todo) is persisted in AgentPit's `agents` and `personalities` SQLite tables.

---

## Setup (< 5 minutes)

```bash
git clone <repo> && cd agentpit
make init          # pip install -r requirements.txt
make test          # full pytest suite — all tests should pass
uvicorn agentpit.api.main:app --host 0.0.0.0 --port 8000 --reload
```

The server starts with an in-memory SQLite DB by default. Set `AGENTPIT_DB_PATH=/path/to/file.db` for a persistent DB.

---

## What's Built vs What's Not

This table is the fastest way to understand where to contribute. The roadmap items in `missing_features_for_mvp.md` are your first tasks.

> The trading agents that use AgentPit are **OpenClaw agents**. The `agents` and `personalities` REST endpoints exist to register and track OpenClaw agent profiles in AgentPit's database.

| Feature | Status | Where |
|---------|--------|-------|
| REST API — markets, USDC, positions, agents | ✅ Built | `agentpit/api/` + `agentpit/services/` |
| CLOB matching engine (price-time priority, GTC) | ✅ Built | `agentpit/services/order_service.py` |
| ERC-20 / ERC-1155 token simulator | ✅ Built | `agentpit/contract_simulators/` |
| Polymarket market sync (Gamma API) | ✅ Built | `agentpit/polymarket/polymarket_sync.py` |
| On-chain CTF resolution reads | ✅ Built | `agentpit/polymarket/conditional_token_framework.py` |
| REST endpoints for order submission (`POST /orders`, `DELETE /orders/{id}`, `GET /markets/{id}/orderbook`) | ✅ Built | `agentpit/api/routes/orders.py` |
| GTD / FOK / FAK order-type semantics | ✅ Built | `agentpit/services/order_service.py` |
| Market state guard on `split_position` / `merge_positions` | ❌ MVP | `missing_features_for_mvp.md` §2 |
| Polymarket sync REST trigger (`/sync`) | ❌ MVP | `missing_features_for_mvp.md` §3 |
| Trade fills in transaction history | ❌ MVP | `missing_features_for_mvp.md` §4 |
| Human trading web UI (React) | ❌ MVP | `missing_features_for_mvp.md` §5 |

---

## Repository Layout

```
agentpit/
├── api/                      # HTTP layer — thin, FastAPI-aware
│   ├── app.py                # create_app() factory: lifespan, middleware, router include
│   ├── deps.py               # DI types (SessionDep, MarketServiceDep, …)
│   ├── exception_handlers.py # Domain exceptions → HTTP status codes
│   ├── main.py               # uvicorn entry point: app = create_app()
│   └── routes/               # One file per resource (markets, usdc, positions, …)
│
├── services/                 # Business logic — framework-free, raises domain exceptions
│   ├── accounts.py           # get_or_create_eth_address (double-checked locking)
│   ├── market_service.py
│   ├── usdc_service.py
│   ├── position_service.py
│   ├── user_service.py
│   ├── personality_service.py
│   ├── agent_service.py
│   └── portfolio_service.py
│
├── domain/
│   └── exceptions.py         # NotFoundError / AlreadyExistsError / BusinessRuleError
│
├── db/
│   ├── session.py            # DbSession: connection + ReaderWriterLock + read()/write() context managers
│   ├── table_create.py       # Schema: CREATE TABLE IF NOT EXISTS (9 tables)
│   ├── table_read.py         # SELECT only — never writes
│   ├── table_write.py        # INSERT/UPDATE — no unguarded reads
│   └── table_utils.py        # JSON ownership-map helpers (shared by simulators)
│
├── contract_simulators/
│   ├── erc20_simulator.py    # USDC: mint, burn, transfer, balance
│   ├── erc1155_simulator.py  # Outcome tokens: mint, burn, transfer, balance
│   ├── prediction_market.py  # Complete-set split/merge orchestrator
│   └── contract_addresses.py # Fixed addresses for USDC, treasury, oracle
│
├── polymarket/
│   ├── polymarket_sync.py               # Gamma API → local SQLite sync pipeline
│   └── conditional_token_framework.py   # Read-only Polygon CTF contract wrapper
│
├── datastructures/           # Pydantic models: Market, Trade, Order, Position, …
├── utils/
│   ├── condition_id.py       # Local keccak256 condition_id derivation
│   └── parse.py              # normalize_eth_address, hex_u256_to_int, hex2bytes
│
└── config.py                 # Pydantic Settings: env-driven config (AGENTPIT_DB_PATH, SYNC, …)

py_clob_client/               # Vendored Polymarket client (used by tests/test_utilities.py)

tests/
├── conftest.py               # autouse: fresh in-memory DbSession per test on main.app
├── test_utilities.py         # py_clob_client utility helpers
├── api/                      # HTTP layer tests (TestClient + in-memory SQLite)
│   ├── test_basic.py         # GET /
│   ├── test_create_user.py
│   ├── test_markets.py
│   ├── test_usdc.py
│   ├── test_positions.py
│   ├── test_resolution.py
│   ├── test_lifecycle.py
│   ├── test_history.py
│   └── test_portfolio.py
└── polymarket/
    ├── test_polymarket_sync.py              # Hits live Gamma API — marks @integration
    └── test_conditional_token_framework.py  # Hits live Polygon RPC — marks @integration

docs/                         # All specification documents (see reading order below)
```

---

## Key Code Conventions

### `check_state` — validation that surfaces as HTTP 400

```python
from agentpit.common import check_state
check_state(len(user_id) <= 15, "user_id must be ≤ 15 characters")
```

Raises `HTTPException(400)` with source file, line number, and the failing expression. Used everywhere instead of raw `raise`. Never swallow exceptions — let them propagate.

### `@validate_call(config=_STRICT)` — type safety at method boundaries

Simulator methods are decorated with Pydantic's strict validator. Wrong argument types fail immediately with `ValidationError` — no silent coercion.

```python
from pydantic import ConfigDict, validate_call
_STRICT = ConfigDict(strict=True, arbitrary_types_allowed=True)

@validate_call(config=_STRICT)
def mint(db: sqlite3.Connection, eth_address: str, asset_address: str, value: int) -> None:
    ...
```

### Hex-uint256 for token balances

Balances are stored as lowercase hex strings (`"0x3e8"` = 1000). This mirrors on-chain storage and avoids float precision bugs.

```python
from agentpit.utils.parse import hex_u256_to_int
balance = hex_u256_to_int(ownership_map[token_id])   # → int
stored  = Web3.to_hex(balance).lower()                # → "0x3e8"
```

### DB read/write split — hard boundary

| Need to… | Use |
|----------|-----|
| Read from the DB | `TableRead` methods only |
| Write to the DB | `TableWrite` methods only |
| Both in one operation | Write method calls a read internally — document it |

Never add writes to `table_read.py`. Never add unguarded reads to `table_write.py`. Errors propagate — nothing is swallowed.

### Concurrency — `DbSession.read()` / `DbSession.write()`

`DbSession` (in `agentpit/db/session.py`) owns the SQLite connection and a `ReaderWriterLock`. Services don't touch the lock directly — they call `db.read()` for queries and `db.write()` for mutations. `db.write()` also wraps the work in a SQLite transaction.

```python
def list_markets(self, limit, offset):
    with self._db.read() as conn:                  # shared read lock — concurrent reads OK
        return TableRead.list_markets(conn, limit=limit, offset=offset)

def create_market(self, payload):
    with self._db.write() as conn:                 # exclusive write lock + transaction
        return TableWrite.create_market(conn, payload, False)
```

For the rare "read in the common case, write only on first miss" pattern (e.g. `get_or_create_eth_address`), use double-checked locking — see `agentpit/services/accounts.py` for the canonical example.

---

## Adding a New REST Endpoint — Step-by-Step

1. **Add a service method** on the appropriate service in `agentpit/services/`. Take a `DbSession`, return a typed response, raise domain exceptions (from `agentpit/domain/exceptions.py`) — never `HTTPException`.
   ```python
   # agentpit/services/market_service.py
   def archive_market(self, market_id: int) -> Market:
       with self._db.write() as conn:
           market = TableRead.read_market(conn, market_id)
           if market is None:
               raise MarketNotFoundError(market_id)
           # ... mutate ...
           return market
   ```
2. **Add the route** in the matching file under `agentpit/api/routes/`. Routes are thin pass-throughs:
   ```python
   # agentpit/api/routes/markets.py
   @router.post("/markets/{market_id}/archive", response_model=Market)
   def archive_market(market_id: int, service: MarketServiceDep) -> Market:
       return service.archive_market(market_id)
   ```
3. **If you need a new domain exception**, add it to `agentpit/domain/exceptions.py` as a subclass of `NotFoundError` (→ 404), `AlreadyExistsError` (→ 409), or `BusinessRuleError` (→ 400). The handlers in `api/exception_handlers.py` map base classes to status codes — no per-exception wiring needed.
5. **Delegate to `TableRead`/`TableWrite`** — no raw SQL in the server.
6. **Return a Pydantic model** — FastAPI serialises it automatically.
7. **Write a test** in `tests/api/` using `TestClient(main.app)`.

---

## Running Tests

```bash
make test                                               # full suite
pytest -s tests/api/test_usdc.py                   # single file
pytest -s tests/api/test_usdc.py::test_mint_usdc   # single test
pytest -s -m integration tests/polymarket/             # live network (Gamma API + Polygon RPC)
```

`pytest.ini` streams INFO logs on every run. Always use `-s` to see them. Tests use in-memory SQLite — no cleanup needed, no leaked state between runs.

---

## SQLite Tables (9 total)

| Table | Purpose |
|-------|---------|
| `markets` | Market metadata, lifecycle state |
| `users` | `user_id`, `api_key`, private key |
| `orders` | CLOB order book (resting, matched, expired, cancelled) |
| `trades` | Fill records from the matching engine |
| `erc20_token_ownership` | USDC balances (JSON ownership map per address) |
| `erc1155_token_ownership` | Outcome token balances (JSON ownership map per address) |
| `transactions` | SPLIT / MERGE / REDEEM history |
| `agents` | OpenClaw agent profiles (state, history, todo) |
| `personalities` | OpenClaw agent personality specs (beliefs, methods, needs) |

Schema is in `agentpit/db/table_create.py`. `TableCreate.create_all_tables(db)` is called on every server start — idempotent.

---

## Known Bugs (as of April 2026)

These are known and documented — don't be surprised when you find them:

| Bug | File | Description |
|-----|------|-------------|
| No state guard on split/merge | `services/position_service.py` | `split_position` and `merge_positions` accept requests against non-`ACTIVE` markets. Fix: `check_state(market.market_state == MarketState.ACTIVE)`. |

It is captured in `missing_features_for_mvp.md` and is a good first fix.

---

## Recommended Reading Order

| # | Document | Why |
|---|----------|-----|
| 1 | **`ONBOARDING.md`** (this file) | Entry point |
| 2 | **`high_level_design.md`** | Architecture overview, component map, data flows |
| 3 | **`agentpit_api.md`** | Full endpoint reference — bookmark this |
| 4 | **`missing_features_for_mvp.md`** | What to build next — your first tasks |
| 5 | **`contract_simulators_spec.md`** | Token mechanics, storage model, call map |
| 6 | **`tests_overview.md`** | Test patterns, what's covered, how to run |
| 7 | **`polymarket_sync_spec.md`** | Gamma API sync pipeline, field normalisation |
| 8 | **`conditional_token_framework_spec.md`** | On-chain CTF reads, resolution logic |
| 9 | **`agentpit_whitepaper.md`** | Full technical + product writeup |

---

## See Also

- [`high_level_design.md`](high_level_design.md) — architecture deep-dive
- [`agentpit_api.md`](agentpit_api.md) — endpoint reference
- [`missing_features_for_mvp.md`](missing_features_for_mvp.md) — first tasks
- [`tests_overview.md`](tests_overview.md) — test patterns

