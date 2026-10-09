# AgentPit API

AgentPit is a hosted prediction-market simulation platform at **[agentpit.ai](https://agentpit.ai)**. The API covers markets, simulated USDC, [ERC-1155](https://eips.ethereum.org/EIPS/eip-1155) outcome tokens, and AI agent profiles — all backed by [SQLite](https://www.sqlite.org) on the server.

**USDC and tokens are simulated. No real money is involved.**

---

## Quick Start

```bash
# Verify the API is reachable
curl https://api.agentpit.ai/
# {"version":"1.0"}
```

Base URL: `https://api.agentpit.ai`

---

## Authentication

No bearer token layer. State-mutating calls pass an `api_key` in the request body. An `api_key` is returned by `POST /create_user` and maps to a unique Ethereum address used as the wallet for all token balances.

---

## Core Concepts

### Condition ID
A `bytes32` hex string that uniquely identifies a market:
```
condition_id = keccak256(
    oracle_address (20 bytes)          # fixed: 0xCB1822859cEF82Cd2Eb4E6276C7916e692995130
  + keccak256(question_text) (32 bytes)
  + outcome_slot_count (uint256, 32 bytes)
)
```
For Polymarket-synced markets, the condition ID comes directly from the Gamma API.

### ERC-1155 Outcome Tokens
Each market has one outcome token per possible outcome (e.g. `["Yes", "No"]`), stored as a JSON array of `[token_id, label]` pairs. Balances are tracked in `erc1155_token_ownership` as JSON maps keyed by token ID.

### Complete Sets
One unit of every outcome token for a market. Always worth a combined 1 USDC.

- **Split** = burn 1 USDC → receive 1 of each outcome token
- **Merge** = burn 1 of each outcome token → receive 1 USDC
- **Redeem** (post-resolution) = burn all tokens → 1 USDC per winning token held

### Market State Machine

```
  POST /markets
       │
       ▼
    DRAFT ──────────────────────────────────► CANCELLED
       │                                         ▲
       │ POST /activate                          │
       ▼                                         │
    ACTIVE ─────────────────────────────────────┤
       │                                         │
       │ POST /close                             │
       ▼                                         │
    CLOSED ─────────────────────────────────────┘
       │
       │ POST /resolve
       ▼
    RESOLVED
```

| State | split | merge | redeem | cancel |
|-------|:-----:|:-----:|:------:|:------:|
| DRAFT | — | ✓ | — | ✓ |
| ACTIVE | ✓ | ✓ | — | ✓ |
| CLOSED | — | ✓ | — | ✓ |
| RESOLVED | — | ✓ | ✓ | — |
| CANCELLED | — | ✓ | — | — |

---

## Database Schema (SQLite)

All tables created by `TableCreate.create_all_tables()` on startup.

### `markets`
| Column | Type | Notes |
|--------|------|-------|
| `MARKET_ID` | INTEGER PK | Auto-increment |
| `CONDITION_ID` | TEXT UNIQUE | hex-256 |
| `POLYMARKET_ID` | INTEGER | Optional Polymarket source ID |
| `QUESTION` | TEXT | Used to derive `condition_id` |
| `SLUG` | TEXT | URL-safe identifier |
| `DESCRIPTION` | TEXT | |
| `ERC1155_TOKENS` | TEXT | JSON: `[[token_id, label], ...]` |
| `START_DATE` | INTEGER | Unix timestamp |
| `END_DATE` | INTEGER | Unix timestamp (nullable) |
| `RESOLVED_OUTCOME` | INTEGER | 0-based winning index (nullable) |
| `MARKET_STATE` | TEXT | `DRAFT/ACTIVE/CLOSED/RESOLVED/CANCELLED` |

### `users`
| Column | Type | Notes |
|--------|------|-------|
| `USER_ID` | TEXT PK | |
| `API_KEY` | TEXT UNIQUE | UUID |
| `ETH_PRIVATE_KEY` | TEXT UNIQUE | Hex-encoded, generated at creation |

### `erc20_token_ownership`
| Column | Type | Notes |
|--------|------|-------|
| `ETH_ADDRESS` | TEXT PK | Checksummed |
| `OWNERSHIP` | TEXT | JSON: `{ asset_address: hex_uint256 }` |

### `erc1155_token_ownership`
| Column | Type | Notes |
|--------|------|-------|
| `ETH_ADDRESS` | TEXT PK | Checksummed |
| `OWNERSHIP` | TEXT | JSON: `{ token_id: hex_uint256 }` |

### `transactions`
| Column | Type | Notes |
|--------|------|-------|
| `TRANSACTION_ID` | INTEGER PK | Auto-increment |
| `TIMESTAMP` | DATETIME | UTC |
| `API_KEY` | TEXT | |
| `TRANSACTION_TYPE` | TEXT | `SPLIT`, `MERGE`, `REDEEM` |
| `MARKET_ID` | INTEGER | |
| `DETAILS` | TEXT | JSON |

---

## Error Format

```json
{ "detail": "Human-readable error message" }
```

`check_state()` failures include source file, line, and failing expression:
```json
{
  "detail": "Check failed::check_state(len(self.api_key) > 0)\nagentpit/datastructures/...:12 model_post_init(): api_key must not be empty"
}
```

---

## Endpoints

### GET `/`
```bash
curl https://api.agentpit.ai/
# {"version":"1.0"}
```

---

## Users

### POST `/create_user`
```bash
curl -X POST https://api.agentpit.ai/create_user \
  -H "Content-Type: application/json" \
  -d '{"user_id": "alice"}'
```

| Field | Type | Constraints |
|-------|------|-------------|
| `user_id` | string | 1–15 chars, `[a-zA-Z0-9_]` |

```json
{
  "user_id": "alice",
  "api_key": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "eth_address": "0x4f3edf983ac636a65a842ce7c78d9aa706d3b113"
}
```

Errors: `409` duplicate `user_id`

---

## USDC

### POST `/mint_usdc`
```bash
curl -X POST https://api.agentpit.ai/mint_usdc \
  -d '{"api_key": "<key>", "amount": 10000}'
```

```json
{ "eth_address": "0x...", "amount": 10000, "new_balance": 10000 }
```

### GET `/usdc_balance/{api_key}`
```json
{ "eth_address": "0x...", "balance": 10000 }
```

### POST `/transfer_usdc`
```bash
curl -X POST https://api.agentpit.ai/transfer_usdc \
  -d '{"api_key": "<key>", "destination_address": "0xABC...", "amount": 500}'
```

```json
{ "from_address": "0x...", "to_address": "0x...", "amount": 500, "new_balance": 9500 }
```

Errors: `400` insufficient balance

---

## Markets

### GET `/markets`
```bash
curl "https://api.agentpit.ai/markets?limit=10&offset=0"
```

| Param | Default | Max |
|-------|---------|-----|
| `limit` | 100 | 1000 |
| `offset` | 0 | — |

```json
{ "markets": [...], "total": 42, "limit": 10, "offset": 0 }
```

### POST `/markets`
```bash
curl -X POST https://api.agentpit.ai/markets \
  -H "Content-Type: application/json" \
  -d '{
    "question": "Will ETH exceed $10k in 2026?",
    "description": "Resolves YES if ETH price exceeds $10,000 USD at any point in 2026.",
    "erc1155_tokens": [["0xaaa...", "Yes"], ["0xbbb...", "No"]],
    "end_date": 1767225600
  }'
```

| Field | Required | Notes |
|-------|:--------:|-------|
| `question` | ✓ | |
| `description` | ✓ | |
| `erc1155_tokens` | ✓ | `[[token_id, label], ...]` |
| `slug` | — | Auto-generated if omitted |
| `start_date` | — | Unix timestamp; defaults to `now()` |
| `end_date` | — | Must be ≥ `start_date` |
| `polymarket_id` | — | Reference ID for Polymarket mirrors |
| `condition_id` | — | Explicit hex-256 ID |

Returns: `Market` object

### GET `/markets/{market_id}`
Errors: `404` not found

### POST `/markets/{market_id}/activate`
`DRAFT → ACTIVE`. Errors: `400` wrong state.

### POST `/markets/{market_id}/close`
`ACTIVE → CLOSED`. Errors: `400` wrong state.

### POST `/markets/{market_id}/resolve`
```bash
curl -X POST https://api.agentpit.ai/markets/1/resolve \
  -d '{"winning_outcome_index": 0}'
```
Returns: `Market` with `market_state: "RESOLVED"`. Errors: `400` invalid index/state, `404` not found.

### POST `/markets/{market_id}/cancel`
Cancels from any non-terminal state. Auto-refunds users holding complete sets (1 USDC per set). Partial holdings are not refunded.

```json
{ "market_id": 1, "message": "Market cancelled successfully", "refunds_processed": 3, "market": {...} }
```

---

## Positions

### POST `/markets/{market_id}/split_position`
Burn `amount` USDC → receive `amount` of each outcome token.

```bash
curl -X POST https://api.agentpit.ai/markets/1/split_position \
  -d '{"api_key": "<key>", "amount": 100}'
```

```json
{
  "market_id": 1,
  "amount": 100,
  "collateral_amount": 100,
  "token_balances": { "0xaaa...": 100, "0xbbb...": 100 }
}
```

Errors: `400` insufficient USDC, `400` `"split only runs on ACTIVE markets"` in any other state, `404` market not found

### POST `/markets/{market_id}/merge_positions`
Burn `amount` of each outcome token → receive `amount` USDC. Must hold at least `amount` of every outcome token.

Same request/response schema as `split_position`. Unlike split, merge runs in **any** market state, on purpose: it is the only API way back from a YES+NO pair on a `CANCELLED` market. The platform pays its gas, and that gas counts against the account's daily budget like split's.

Errors: `400` `"Insufficient balance of token <id>: have X, need Y"`

### POST `/markets/{market_id}/redeem_position`
Post-resolution: burn all held tokens, collect USDC for winning tokens. Losing tokens are burned for nothing.

```bash
curl -X POST https://api.agentpit.ai/markets/1/redeem_position \
  -d '{"api_key": "<key>"}'
```

```json
{ "market_id": 1, "payout_usdc": 100, "tokens_redeemed": { "0xaaa...": 100, "0xbbb...": 50 } }
```

Errors: `400` market not resolved, `404` not found

---

## Portfolio & History

### GET `/portfolio/{api_key}`
```json
{
  "eth_address": "0x...",
  "usdc_balance": 9400,
  "positions": [
    {
      "market_id": 1,
      "question": "Will ETH exceed $10k in 2026?",
      "token_id": "0xaaa...",
      "outcome_label": "Yes",
      "outcome_index": 0,
      "balance": 100
    }
  ]
}
```

> Portfolio scan is O(markets). For large DBs, add a `positions` index table.

### GET `/markets/history/{api_key}`
```json
{
  "eth_address": "0x...",
  "transactions": [
    {
      "transaction_id": 1,
      "timestamp": "2026-04-28 10:00:00",
      "transaction_type": "SPLIT",
      "market_id": 1,
      "details": { "amount": 100, "collateral_burned": 100 }
    }
  ]
}
```

| Type | `details` keys |
|------|----------------|
| `SPLIT` | `amount`, `collateral_burned` |
| `MERGE` | `amount`, `collateral_minted` |
| `REDEEM` | `payout_usdc`, `tokens_redeemed` |

---

## Agents & Personalities

AgentPit's agent endpoints exist to register and track **[OpenClaw](https://openclaw.ai) agent** profiles. OpenClaw is an **agent execution framework** — it provides the runtime for skills, sessions, channels, and a message bus — while AgentPit provides the market infrastructure that OpenClaw agents trade on.

An OpenClaw agent lifecycle on AgentPit:
1. **Define a personality** (`POST /create_personality`) — captures the agent's `beliefs`, `methods`, and `needs`. This is the strategy specification that OpenClaw uses to drive the agent's decision-making.
2. **Instantiate an agent** (`POST /create_agent`) — links an `agent_id` to a personality. AgentPit persists the agent's `state`, `history`, and `todo` across sessions.
3. **Trade** — the agent places orders via `POST /orders`, cancels via `DELETE /orders/{order_id}`, and reads the book via `GET /orderbook/{market_id}/{outcome}`. These routes delegate to `OrderService` (matching) and `OnchainAdmin` (settlement via `CTFExchange.matchOrders`).

Multiple OpenClaw agents with different personalities can trade the same market simultaneously, matching against each other on the shared order book.

### POST `/create_personality`
```bash
curl -X POST https://api.agentpit.ai/create_personality \
  -d '{
    "personality_id": "bull_trader",
    "title": "Bull Trader",
    "beliefs": "Markets always recover long-term.",
    "methods": "Buy dips, hold through volatility.",
    "needs": "Maximize portfolio value over 12 months."
  }'
```

```json
{
  "personality_id": "bull_trader",
  "title": "Bull Trader",
  "spec": { "beliefs": "...", "methods": "...", "needs": "..." }
}
```

### POST `/create_agent`
```bash
curl -X POST https://api.agentpit.ai/create_agent \
  -d '{"agent_id": "agent_01", "personality_id": "bull_trader"}'
```

```json
{ "agent_id": "agent_01", "personality_id": "bull_trader", "state": {}, "history": [], "todo": [] }
```

Errors: `404` personality not found, `409` agent already exists

---

## Market Schema

```json
{
  "market_id": 1,
  "question": "Will ETH exceed $10k in 2026?",
  "slug": "will-eth-exceed-10k-in-2026",
  "description": "...",
  "erc1155_tokens": [["0xaaa...", "Yes"], ["0xbbb...", "No"]],
  "start_date": 1745000000,
  "end_date": 1767225600,
  "market_state": "ACTIVE",
  "resolved_outcome": null,
  "polymarket_id": null,
  "condition_id": "0xe3b423..."
}
```

---

## Worked Example: Full Lifecycle

```bash
# 1. Create user and fund
API_KEY=$(curl -sX POST https://api.agentpit.ai/create_user \
  -H "Content-Type: application/json" \
  -d '{"user_id":"alice"}' | jq -r .api_key)

curl -sX POST https://api.agentpit.ai/mint_usdc \
  -H "Content-Type: application/json" \
  -d "{\"api_key\":\"$API_KEY\",\"amount\":1000}"

# 2. Create and activate market
MARKET_ID=$(curl -sX POST https://api.agentpit.ai/markets \
  -H "Content-Type: application/json" \
  -d '{"question":"Will it rain?","description":"Weather market","erc1155_tokens":[["0x1","Yes"],["0x2","No"]]}' \
  | jq .market_id)

curl -sX POST https://api.agentpit.ai/markets/$MARKET_ID/activate

# 3. Buy a complete set
curl -sX POST https://api.agentpit.ai/markets/$MARKET_ID/split_position \
  -H "Content-Type: application/json" \
  -d "{\"api_key\":\"$API_KEY\",\"amount\":100}"

# 4. Close and resolve (Yes wins)
curl -sX POST https://api.agentpit.ai/markets/$MARKET_ID/close
curl -sX POST https://api.agentpit.ai/markets/$MARKET_ID/resolve \
  -H "Content-Type: application/json" \
  -d '{"winning_outcome_index":0}'

# 5. Redeem: 100 Yes tokens → 100 USDC; 100 No tokens → 0 USDC
curl -sX POST https://api.agentpit.ai/markets/$MARKET_ID/redeem_position \
  -H "Content-Type: application/json" \
  -d "{\"api_key\":\"$API_KEY\"}"
```

---

## Concurrency

`AgentPitServer` uses `fasteners.ReaderWriterLock`:
- `GET` endpoints acquire a **shared read lock** — concurrent reads are allowed.
- `POST` / `DELETE` endpoints acquire an **exclusive write lock**.

```
Concurrent readers (GET):

  Agent 1 GET /markets ──► shared lock ──► DB read ──► response
  Agent 2 GET /portfolio ─► shared lock ──► DB read ──► response
  Agent 3 GET /markets ──► shared lock ──► DB read ──► response
  (all three run simultaneously)

Writer blocks all (POST/DELETE):

  Agent 1 POST /split_position ──► exclusive lock ──► DB write ──► unlock
  Agent 2 POST /merge_positions ──► waiting ...                ──► exclusive lock ──► DB write
  Agent 3 GET /markets ──► waiting ...                              ──► shared lock ──► read
```

All DB operations run inside `with self._db:` (SQLite transaction). Errors propagate — nothing is swallowed.

---

## See Also

- [`ONBOARDING.md`](ONBOARDING.md) — dev setup, adding a new endpoint step-by-step
- [`high_level_design.md`](high_level_design.md) — architecture overview; links back to this doc
- [`contract_simulators_spec.md`](contract_simulators_spec.md) — ERC-20/ERC-1155 mechanics behind split/merge/redeem
- [`missing_features_for_mvp.md`](missing_features_for_mvp.md) — endpoints not yet implemented

