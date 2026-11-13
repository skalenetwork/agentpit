# Polymarket Sync — Design Specification
## Purpose
`agentpit/polymarket/polymarket_sync.py` keeps the local AgentPit [SQLite](https://www.sqlite.org) database in sync with live [Polymarket](https://polymarket.com) data. It bridges two external systems:

The markets it imports are the markets that **[OpenClaw](https://openclaw.ai) agents** trade on. OpenClaw is an agent execution framework; `polymarket_sync` is what ensures those agents are trading real, live Polymarket questions at accurate market-implied odds rather than synthetic data.
 System  Role
--------------
 **[Polymarket Gamma API](https://gamma-api.polymarket.com)**  Source of truth for market metadata (questions, tokens, dates, slugs)
 **[Polymarket CLOB API](https://docs.polymarket.com)**  Source of truth for per-market `closed` status
 **[Polygon CTF contract](https://polygonscan.com/address/0x4D97DCd97eC945f40cF65F87097ACe5EA0476045)**  Source of truth for on-chain resolution and payout data
The sync is one-directional: Polymarket → local DB. The local DB is never written back to Polymarket.
---
## When to Run Sync
Sync is not automatic on server start. You call it explicitly:
```python
from agentpit.polymarket.polymarket_sync import fetch_and_sync_polymarket_markets
created = fetch_and_sync_polymarket_markets(db)
print(f"{len(created)} new markets added")
```
Or schedule it with `nanobot/cron/` for periodic updates (e.g. every 15 minutes).
---
## External Dependencies
| Dependency | URL | Purpose |
|------------|-----|---------|
| [Polymarket Gamma API](https://gamma-api.polymarket.com) | `https://gamma-api.polymarket.com` | Market metadata (question, description, tokens, dates, condition_id) |
| [Polymarket CLOB API](https://docs.polymarket.com) | `https://clob.polymarket.com/markets` | Per-market `closed` flag |
| [Polygon CTF Contract](https://polygonscan.com/address/0x4D97DCd97eC945f40cF65F87097ACe5EA0476045) | `0x4D97DCd97eC945f40cF65F87097ACe5EA0476045` | On-chain condition existence check and payout resolution |
| Polygon RPC | `https://tenderly.rpc.polygon.community` | Web3 provider for CTF contract calls |
HTTP logs from `httpx` and `httpcore` are suppressed to `WARNING` to reduce noise from per-request logs.
---
## Top-Level Function: `fetch_and_sync_polymarket_markets(db, host)`
This is the entry point for a full sync. It:
1. Fetches all live Polymarket markets via `fetch_all_polymarket_markets()`.
2. For each market, creates a local DB record if one doesn't already exist, after verifying the condition exists on-chain.
3. Iterates every local market that has a `polymarket_id` and calls `sync_market_state()` to update its `MARKET_STATE`.
**Returns:** list of newly created `Market` objects. Returns `[]` if all markets already exist (idempotent).
```python
# First call: inserts all discovered markets
created = fetch_and_sync_polymarket_markets(db)  # → [Market, Market, ...]
# Second call: no-op (markets already exist)
created = fetch_and_sync_polymarket_markets(db)  # → []
```
---
## Stage 1 — Fetching Markets from Gamma API
### `fetch_all_polymarket_markets(host, closed, active, archived, liquidity_threshold)`
Paginates through `GET /markets` on the Gamma API in batches of 500 until fewer than 500 results are returned (end-of-data signal).
**Default call parameters sent to Gamma:**
| Parameter | Default | Description |
|-----------|---------|-------------|
| `active` | `true` | Only active markets |
| `closed` | `false` | Exclude closed/resolved markets |
| `archived` | `false` | Exclude archived markets |
| `limit` | `500` | Page size |
| `offset` | incremented | Pagination cursor |
**Client-side filters applied after each page (not sent to Gamma):**
| Filter | Condition | Reason |
|--------|-----------|--------|
| No `condition_id` | Skip | Cannot create a market without a condition ID |
| `liquidity < 1_000_000` | Skip | Ignore illiquid markets; threshold configurable |
| `archived=true` | Raise `ValueError` | Gamma leaking archived markets despite `archived=false` is a data error |
| `closed=true` (when `closed=false` requested) | Skip | Gamma can leak closed markets |
| `end_date_iso` in the past | Skip | Expired markets are excluded unless `closed=True` is passed |
**Returns:** `list[dict]` of normalized market dicts.
---
### `fetch_polymarket_market(condition_id, host)`
Fetches a **single** market from Gamma by `conditionId`:
```
GET /markets?conditionId=0x...
```
Gamma returns a list; this function picks the first entry whose `condition_id` (after normalization) is an exact case-insensitive match.
Returns `None` if no match is found. Logs a warning if Gamma returns multiple results.
---
## Stage 2 — Field Normalization
The Gamma API uses inconsistent field names (camelCase vs snake_case, multiple aliases for the same concept). All raw market dicts are passed through `_normalize_market_fields()` before any processing.
### `_normalize_market_fields(market) → dict`
Applies `_coalesce_key()` to unify aliases into canonical snake_case keys:
| Canonical key | Aliases checked (first non-None wins) |
|---------------|---------------------------------------|
| `condition_id` | `conditionId` |
| `question` | `title`, `name` |
| `description` | `descriptionText` |
| `polymarket_id` | `id`, `marketId` |
| `end_date_iso` | `endDateIso`, `endDateISO`, `endDate`, `endTimeIso`, `endTime` |
| `active` | `isActive` |
| `closed` | `isClosed` |
| `archived` | `isArchived` |
| `liquidity` | `liquidityNum`, `liquidityClob` |
Boolean fields (`active`, `closed`, `archived`) are coerced by `_to_bool()`:
| Input | Output |
|-------|--------|
| `True` / `False` | as-is |
| `"true"`, `"1"`, `"yes"` | `True` |
| `"false"`, `"0"`, `"no"` | `False` |
| `1` / `0` | `True` / `False` |
| anything else | `None` (field left as-is) |
`polymarket_id` is cast to `int` (or `None` on failure).
After normalization, `_ensure_tokens()` is called.
---
### `_ensure_tokens(market)`
Guarantees `market["tokens"]` is a non-empty list. If `tokens` is absent or empty, it reconstructs the list from two parallel arrays:
- `clobTokenIds` (or `clobTokenids` — case variant) — list of token ID strings
- `outcomes` — list of outcome label strings
Each index pair becomes:
```python
{"token_id": str(token_id), "outcome": str(label)}
```
If `clobTokenIds` is also absent, `tokens` is set to `[]` and the market will be skipped downstream (no tokens = cannot create market).
`_parse_list_field()` handles both native Python lists and JSON-encoded strings in these fields.
---
### `_is_market_expired(market) → bool`
Returns `True` if the market's `end_date_iso` is before UTC now. Handles the `Z` suffix for UTC (`"2024-02-01T00:00:00Z"` → `"2024-02-01T00:00:00+00:00"`).
Returns `False` if `end_date_iso` is missing or unparseable.
---
## Stage 3 — Creating Local Markets
### `create_polymarket_markets_if_needed(db, pm_markets)`
Iterates the normalized market list and calls `create_polygon_market_if_does_not_exist()` for each. Logs a summary line on completion:
```
INFO Synced 12/150 Polymarket markets locally
```
(12 new, 138 already existed.)
---
### `create_polygon_market_if_does_not_exist(db, pm_market) → Market | None`
Per-market creation logic:

```
build_create_market_request_from_json(pm_market)
        │
        ▼
polymarket_id is set?
        │ no → skip
        │ yes
        ▼
CTF.condition_exists(condition_id)?
        │ no → skip
        │ yes
        ▼
TableRead: market already in DB by polymarket_id?
        │ yes → skip (idempotent)
        │ no
        ▼
TableWrite.create_market(request, is_polygon_market=True)
        │
        ▼
return new Market
```

`is_polygon_market=True` tells `TableWrite.create_market` to use the condition ID from the request directly rather than computing one from question text.
---
### `build_create_market_request_from_json(pm_market) → CreateMarketRequest`
Maps normalized Gamma fields to `CreateMarketRequest`:
| `CreateMarketRequest` field | Gamma source field | Notes |
|-----------------------------|--------------------|-------|
| `question` | `question` | `.strip()` applied |
| `description` | `description` | `.strip()` applied |
| `polymarket_id` | `id` | Must be set |
| `erc1155_tokens` | `tokens` | via `_polymarket_to_erc1155_tokens()` |
| `slug` | `slug` | |
| `start_date` | `startDate` | ISO string → Unix via `_iso_to_unix()` |
| `end_date` | `endDate` | ISO string → Unix; `None` if absent |
| `condition_id` | `conditionId` | Wrapped in `ConditionId(...)` |
| `state` | `active` + `closed` | `ACTIVE` if `active=True and closed=False`, else `CLOSED` |
### `_polymarket_to_erc1155_tokens(pm_market) → list[tuple[str, str]]`
Converts `tokens` list to AgentPit's `erc1155_tokens` format:
```python
# Input from Gamma:
[{"token_id": "123...", "outcome": "Yes"}, {"token_id": "456...", "outcome": "No"}]
# Output for CreateMarketRequest:
[("123...", "Yes"), ("456...", "No")]
```
Handles both `token_id` (snake_case) and `tokenId` (camelCase) field names.
---
## Stage 4 — Syncing Market State
### `sync_market_state(db, condition_id)`
Called for every existing local market that has a `polymarket_id`. Checks two sources and applies state transitions:

```
sync_market_state(db, condition_id)
        │
        ├─ local state is DRAFT or ACTIVE?
        │       │ yes
        │       ▼
        │  CLOB API: fetch_is_polymarket_market_closed(condition_id)
        │       │ closed=True              │ closed=False
        │       ▼                          │ (skip)
        │  update_market_state_to_closed   │
        │                                  │
        ├─ local state is DRAFT, ACTIVE, or CLOSED?
        │       │ yes
        │       ▼
        │  CTF: get_onchain_resolution_status(condition_id)
        │       │ resolved=True            │ resolved=False
        │       ▼                          │ (skip)
        │  update_market_state_to_resolved │
        │  (winner_index)                  │
        │                                  │
        └─ done
```

Both checks run in the same call. A market can move `ACTIVE → CLOSED → RESOLVED` in a single invocation if the CTF already shows resolution.
---
### `fetch_is_polymarket_market_closed(condition_id) → bool`
Queries the CLOB API:
```
GET https://clob.polymarket.com/markets/\{condition_id\}
```
The CLOB may return a single dict or a list. The function handles both shapes, normalizes fields with `_normalize_market_fields()`, and returns the coerced `closed` boolean. Raises via `check_state` if the response is missing or malformed.
---
## Full Pipeline Diagram
```
fetch_and_sync_polymarket_markets(db)
│
├─► fetch_all_polymarket_markets()
│       │
│       │  GET /markets?active=true&closed=false&archived=false&limit=500&offset=N
│       │  (paginated until < 500 results)
│       │
│       └─► per page: _normalize_market_fields()
│                     _ensure_tokens()
│                     filter: no condition_id, low liquidity,
│                             archived leak, closed leak, expired
│
├─► create_polymarket_markets_if_needed(db, pm_markets)
│       │
│       └─► per market: build_create_market_request_from_json()
│                       CTF.condition_exists()? ──── No ──► skip
│                       DB: already exists?    ──── Yes ──► skip
│                       TableWrite.create_market(is_polygon_market=True)
│
└─► for each local market with polymarket_id:
        sync_market_state(db, condition_id)
            │
            ├─ CLOB API → closed? → update_market_state_to_closed_if_needed()
            └─ CTF → resolved? → update_market_state_to_resolved_if_needed(winner_index)
```
---
## Local Market State Transitions (driven by sync)
```
DRAFT ──► ACTIVE ──► CLOSED ──► RESOLVED
```
`CANCELLED` is only set locally (via `POST /markets/{id}/cancel`), never by sync.
---
## Environment / Configuration
All constants are module-level — no config file required:
| Constant | Value |
|----------|-------|
| `POLYMARKET_GAMMA_URL` | `https://gamma-api.polymarket.com` |
| `CLOB_MARKET_URL` | `https://clob.polymarket.com/markets` |
CTF constants (`CTF_ADDRESS`, `POLYGON_RPC`) live in `conditional_token_framework.py` — see `conditional_token_framework_spec.md`.
---
## Running Sync Manually
```python
import sqlite3
from agentpit.db.table_create import TableCreate
from agentpit.polymarket.polymarket_sync import fetch_and_sync_polymarket_markets
db = sqlite3.connect("agentpit.db")
TableCreate.create_all_tables(db)
created = fetch_and_sync_polymarket_markets(db)
print(f"Created {len(created)} new markets")
db.close()
```
Or point at a running server's DB:
```bash
AGENTPIT_DB_PATH=/path/to/agentpit.db python -c "
import sqlite3
from agentpit.polymarket.polymarket_sync import fetch_and_sync_polymarket_markets
db = sqlite3.connect('/path/to/agentpit.db')
print(len(fetch_and_sync_polymarket_markets(db)), 'markets synced')
"
```
---
## Tests
### `tests/polymarket/test_polymarket_sync.py`
| Test | Description |
|------|-------------|
| `test_sync_polymarket_markets_syncs_real_markets_to_db` | Hits live Gamma API, verifies `> 5` markets created, checks DB integrity |
| `test_sync_polymarket_markets_syncs_real_markets_to_db` (re-run) | Second call returns `[]` — idempotency check |
Helper unit tests (mock-based) also cover `_is_market_expired`, `_polymarket_to_erc1155_tokens`, `fetch_polymarket_market`.
### `tests/polymarket/test_conditional_token_framework.py`
Integration test using a real Polygon RPC. Marked `@pytest.mark.integration`.
```bash
# Run all sync tests (hits live API)
pytest -s tests/polymarket/test_polymarket_sync.py
# Run only integration tests (hits live Polygon RPC)
pytest -s -m integration tests/polymarket/
```
---
## Known Limitations & Gotchas
| Issue | Details |
|-------|---------|
| **Gamma leaks closed markets** | Even with `closed=false`, Gamma sometimes returns closed markets. Filtered client-side. |
| **Expiry is client-side only** | Gamma doesn't reliably filter by end date. `_is_market_expired()` applies UTC comparison locally. |
| **No CTF caching** | Every `sync_market_state()` call creates a new `Web3` instance. For large DBs, this is slow. |
| **Portfolio scan is O(markets)** | `GET /portfolio/{api_key}` scans all markets to match token IDs. Acceptable for test DBs; add an index for production. |
| **`clobTokenids` vs `clobTokenIds`** | Gamma has a case inconsistency in this field name. Both are handled. |

---

## See Also

- [`ONBOARDING.md`](ONBOARDING.md) — dev setup and first-contribution guide
- [`high_level_design.md`](high_level_design.md) — Polymarket sync component overview and data flow diagram
- [`conditional_token_framework_spec.md`](conditional_token_framework_spec.md) — `CTF.condition_exists()` and `get_onchain_resolution_status()` called by this module
- [`tests_overview.md`](tests_overview.md) — `test_polymarket_sync.py` integration test details

