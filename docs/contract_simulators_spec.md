# Contract Simulators — Design Specification
## Purpose
`agentpit/contract_simulators/` provides in-process, [SQLite](https://www.sqlite.org)-backed simulations of Ethereum smart contracts. They give the AgentPit server real [ERC-20](https://eips.ethereum.org/EIPS/eip-20) and [ERC-1155](https://eips.ethereum.org/EIPS/eip-1155) token semantics — minting, burning, transferring, balance queries — without any blockchain connection or gas costs.

**[OpenClaw](https://openclaw.ai) agents** interact with these simulators indirectly: when an OpenClaw agent calls `POST /split_position`, `POST /merge_positions`, or `POST /redeem_position` via the AgentPit REST API, `AgentPitServer` delegates to these simulators to update the agent's simulated USDC and outcome token balances. OpenClaw is an agent execution framework; the simulators are the token ledger it trades against.

> **These are not real contracts.** No Web3 calls are made. State lives entirely in SQLite rows.
---
## Module Structure
```
agentpit/contract_simulators/
├── contract_addresses.py   # Fixed addresses for simulated assets
├── erc20_simulator.py      # ERC-20 token operations (USDC)
├── erc1155_simulator.py    # ERC-1155 token operations (outcome tokens)
└── prediction_market.py    # Higher-level split/merge using both simulators
```
Supporting layer:
```
agentpit/db/
└── table_utils.py          # Raw JSON ownership map read/write (shared by both simulators)
```

**Layer relationships:**
```
AgentPitServer
    │
    ├── ERC20Simulator    ──► erc20_token_ownership   (SQLite)
    │       └── TableUtils  (JSON ownership map helpers)
    │
    ├── ERC1155Simulator  ──► erc1155_token_ownership (SQLite)
    │       └── TableUtils
    │
    └── PredictionMarket  (higher-level split/merge orchestration)
            ├── ERC20Simulator   (USDC transfer: owner ↔ treasury)
            └── ERC1155Simulator (outcome token mint/burn)
```

---
## Simulated Contract Addresses
Defined in `contract_addresses.py`. All are fixed strings validated by Pydantic at import time.
| Constant | Address | Role |
|----------|---------|------|
| `EASYNET_USDC_TOKEN_ADDRESS` | `0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359` | Simulated USDC token |
| `EASYNET_MARKET_TREASURY_ADDRESS` | `0x742d35Cc6634C0532925a3b844Bc454e4438f44e` | Holds USDC collateral during a split |
| `EASYNET_ORACLE_ADDRESS` | `0xCB1822859cEF82Cd2Eb4E6276C7916e692995130` | Used to compute local `condition_id` values |
---
## Storage Model
Both simulators store balances in SQLite as **JSON ownership maps**. Each map is a dict serialised to a single `OWNERSHIP` TEXT column.
### ERC-20 (`erc20_token_ownership`)
| Column | Content |
|--------|---------|
| `ETH_ADDRESS` | Normalised checksummed holder address (PK) |
| `OWNERSHIP` | `{ "<asset_address>": "<hex_uint256_balance>", ... }` |
Example row:
```
ETH_ADDRESS  0x742d35cc6634c0532925a3b844bc454e4438f44e
OWNERSHIP    {"0x3c499c542cef5e3811e1192ce70d8cc03d5c3359":"0x3e8"}
```
`0x3e8` = 1000 in hex.
### ERC-1155 (`erc1155_token_ownership`)
| Column | Content |
|--------|---------|
| `ETH_ADDRESS` | Normalised checksummed holder address (PK) |
| `OWNERSHIP` | `{ "<token_id>": "<hex_uint256_balance>", ... }` |
Example row:
```
ETH_ADDRESS  0x742d35cc6634c0532925a3b844bc454e4438f44e
OWNERSHIP    {"0xaaa...":"0x64","0xbbb...":"0x64"}
```
100 of each outcome token in hex.
### Address Normalisation
All Ethereum addresses are passed through `agentpit.utils.parse.normalize_eth_address()` before use as map keys. This lowercases and checksums them so `0xABC...` and `0xabc...` resolve to the same record.
### Balance Encoding
Balances are stored as lowercase hex strings (`Web3.to_hex(n).lower()`). They are decoded back to `int` via `agentpit.utils.parse.hex_u256_to_int()`. Max value is `2^256 - 1`; overflow raises `OverflowError`.
---
## `TableUtils` — Low-Level DB Access
`agentpit/db/table_utils.py` — shared by both simulators.
### `ensure_erc20_ownership_row(db, eth_address)`
`INSERT OR IGNORE` a row with `OWNERSHIP = '{}'` for the address. Safe to call multiple times.
### `load_erc20_ownership_map(db, eth_address) → dict[str, str]`
Reads and JSON-parses the `OWNERSHIP` column. Strict validation:
- Raises `ValueError` if the stored value is not a JSON dict.
- Raises `ValueError` if any key or value in the map is not a `str`.
- Returns `{}` if the row doesn't exist or is NULL (no silent `None` returns).
### `store_erc20_ownership_map(db, eth_address, m)`
Serialises `m` to compact JSON (`separators=(",",":")`) and `UPDATE`s the row.
The ERC-1155 variants (`ensure_erc1155_ownership_row`, `load_erc1155_ownership_map`, `store_erc1155_ownership_map`) are identical but operate on the `erc1155_token_ownership` table.
---
## `ERC20Simulator`
`agentpit/contract_simulators/erc20_simulator.py`
Simulates a single fungible token contract (USDC). All methods are `@staticmethod` and decorated with `@validate_call(config=_STRICT)` — Pydantic rejects wrong types at the boundary.
---
### `mint(db, eth_address, asset_address, value) → None`
Credits `value` units of `asset_address` token to `eth_address`.
```
ensure_erc20_ownership_row(eth_address)
current = load map → map[asset_address] (or 0)
new = current + value
assert new < 2^256  (raises OverflowError)
store map[asset_address] = hex(new)
```
- `value == 0` → no-op (returns immediately)
- Wrapped in `with db:` for an atomic SQLite transaction
**Called by:** `AgentPitServer.mint_usdc()`, `AgentPitServer.merge_positions()`, `AgentPitServer.redeem_position()`, `PredictionMarket.mergeInDbEIP1155TokensIntoUSDC()`
---
### `burn(db, eth_address, asset_address, value) → None`
Debits `value` from `eth_address`'s balance of `asset_address`.
```
load map → current balance
assert current >= value  (raises ValueError: "Insufficient balance: X < Y")
store map[asset_address] = hex(current - value)
```
- `value == 0` → no-op
**Called by:** `AgentPitServer.split_position()`, `PredictionMarket.splitInDbUSDCIntoEIP155Tokens()`
---
### `transfer(db, src_address, destination_address, value, asset_address) → None`
Moves `value` units of `asset_address` from `src_address` to `destination_address`. Atomically updates both maps.
```
load src_map, dst_map
assert src_map[asset] >= value  (raises ValueError: "Insufficient balance: X < Y")
src_map[asset] -= value
dst_map[asset] += value
assert dst_map[asset] < 2^256  (raises OverflowError)
store both maps
```
- `value == 0` or `src == dst` → no-op
**Called by:** `AgentPitServer.transfer_usdc()`, `PredictionMarket.splitInDbUSDCIntoEIP155Tokens()` (user → treasury), `PredictionMarket.mergeInDbEIP1155TokensIntoUSDC()` (treasury → user)
---
### `get_balance(db, eth_address, asset_address) → int`
Returns the current balance as a Python `int`. Returns `0` if the address has no record or the asset is not in the map.
**Called by:** `AgentPitServer.get_usdc_balance()`, `AgentPitServer.mint_usdc()` (post-mint check), all split/merge/redeem flows.
---
## `ERC1155Simulator`
`agentpit/contract_simulators/erc1155_simulator.py`
Simulates a multi-token contract where each `token_id` represents a distinct outcome token. The interface mirrors `ERC20Simulator` exactly, with one conceptual difference: instead of `asset_address` mapping to a fungible token, each `asset_address` argument is the **token ID** of an outcome token.
> **Note:** The parameter names in the source (`eth_address` / `asset_address`) are misleading for ERC-1155 — internally they map to holder address and token ID respectively. The storage key in the JSON map is the `token_id`, not a checksummed asset address.
All four operations — `mint`, `burn`, `transfer`, `get_balance` — are structurally identical to `ERC20Simulator` but read/write the `erc1155_token_ownership` table.
| Operation | Raises on |
|-----------|-----------|
| `mint` | `OverflowError` if new balance ≥ 2^256 |
| `burn` | `ValueError("Insufficient balance: X < Y")` if balance < value |
| `transfer` | `ValueError("Insufficient balance: X < Y")` if source short; `OverflowError` on destination overflow |
| `get_balance` | Never raises; returns 0 for unknown addresses/token IDs |
**Called by:** `AgentPitServer.split_position()` (mint), `AgentPitServer.merge_positions()` (burn check + burn), `AgentPitServer.redeem_position()` (burn), `AgentPitServer.cancel_market()` (burn + refund flow), `PredictionMarket.*`
---
## `PredictionMarket`
`agentpit/contract_simulators/prediction_market.py`
A higher-level orchestrator that composes `ERC20Simulator` and `ERC1155Simulator` to implement complete-set split and merge. It acts as the simulation equivalent of the Gnosis CTF `splitPosition` / `mergePositions` contract calls.
All methods use `@validate_call(config=_STRICT)` (Pydantic strict mode).
---
### `splitInDbUSDCIntoEIP155Tokens(db, owner_address, market_id, usdc_amount)`
**Split:** convert USDC into one complete set of outcome tokens.
```
1. assert usdc_amount > 0
2. owner_usdc = ERC20Simulator.get_balance(owner, USDC)
3. assert owner_usdc >= usdc_amount  (raises via check_state)
4. ERC20Simulator.transfer(owner → TREASURY, usdc_amount, USDC)
5. market = TableRead.read_market(market_id)
6. assert market is not None
7. for each (token_id, _) in market.erc1155_tokens:
       ERC1155Simulator.mint(owner, token_id, usdc_amount)
```
The USDC moves to `EASYNET_MARKET_TREASURY_ADDRESS`, which acts as collateral escrow. One of each outcome token is minted to the owner.
**Note:** The `AgentPitServer.split_position()` endpoint uses `ERC20Simulator.burn` + direct ERC-1155 minting instead of calling `PredictionMarket` — both achieve the same economic result but differ in where the USDC goes (burned vs. escrowed in treasury).
---
### `mergeInDbEIP1155TokensIntoUSDC(db, owner_address, market_id, outcome_token_amount)`
**Merge:** return a complete set of outcome tokens and recover USDC.
```
1. assert outcome_token_amount > 0
2. market = TableRead.read_market(market_id)
3. assert market is not None
4. for each (token_id, _) in market.erc1155_tokens:
       balance = ERC1155Simulator.get_balance(owner, token_id)
       assert balance >= outcome_token_amount  (raises via check_state)
5. for each (token_id, _) in market.erc1155_tokens:
       ERC1155Simulator.burn(owner, token_id, outcome_token_amount)
6. ERC20Simulator.transfer(TREASURY → owner, outcome_token_amount, USDC)
```
Step 4 (full pre-check before any burn) ensures atomicity: either all tokens are available and all are burned, or none are burned and the error is raised before any state changes.
---
## Error Handling
| Error | Source | HTTP result |
|-------|--------|-------------|
| `ValueError("Insufficient balance: X < Y")` | `ERC20Simulator.burn/transfer` or `ERC1155Simulator.burn/transfer` | `400` (caught by server and re-raised as `HTTPException`) |
| `OverflowError("u256 overflow")` | Any simulator on `mint`/`transfer` if new balance ≥ 2^256 | Propagates uncaught (crash) — practically impossible with normal amounts |
| `ValueError("Corrupted OWNERSHIP data ...")` | `TableUtils.load_*_ownership_map` | Propagates uncaught — indicates DB corruption |
| `check_state` failure | `PredictionMarket` pre-conditions | `400 HTTPException` with call-site detail |
---
## Concurrency
Each simulator method wraps its DB operations in `with db:` — a SQLite transaction context manager. If any step raises, the transaction rolls back. The `AgentPitServer` additionally holds a `ReaderWriterLock` write lock around all state-changing calls, so only one operation runs at a time.
---
## Call Map
Which server endpoints call which simulator methods:
| Endpoint | ERC20Simulator | ERC1155Simulator |
|----------|---------------|-----------------|
| `POST /mint_usdc` | `mint` | — |
| `GET /usdc_balance/{key}` | `get_balance` | — |
| `POST /transfer_usdc` | `transfer` | — |
| `POST /split_position` | `burn` (USDC) | `mint` (each outcome token) |
| `POST /merge_positions` | `mint` (USDC) | `burn` (each outcome token) |
| `POST /redeem_position` | `mint` (winning payout) | `burn` (all tokens) |
| `POST /cancel` | `mint` (refund per complete set) | `burn` (complete set tokens) |
| `GET /portfolio/{key}` | `get_balance` | (reads ownership map directly) |

---

## See Also

- [`ONBOARDING.md`](ONBOARDING.md) — hex-uint256 storage convention, dev setup
- [`high_level_design.md`](high_level_design.md) — component diagram showing simulator layer
- [`agentpit_api.md`](agentpit_api.md) — endpoint reference for split, merge, redeem, cancel
- [`tests_overview.md`](tests_overview.md) — `test_positions.py`, `test_resolution.py`, `test_lifecycle.py`
---
## Worked Example: Split → Merge Round-trip
```
Initial state:
  Alice USDC balance: 1000
  Alice Yes-token balance: 0
  Alice No-token balance: 0
  Treasury USDC balance: 0
--- split_position(amount=100) ---
  ERC20Simulator.burn(alice, USDC, 100)
    → Alice USDC: 900
  ERC1155Simulator.mint(alice, yes_token_id, 100)
    → Alice Yes: 100
  ERC1155Simulator.mint(alice, no_token_id, 100)
    → Alice No: 100
--- merge_positions(amount=50) ---
  ERC1155Simulator.burn(alice, yes_token_id, 50)
    → Alice Yes: 50
  ERC1155Simulator.burn(alice, no_token_id, 50)
    → Alice No: 50
  ERC20Simulator.mint(alice, USDC, 50)
    → Alice USDC: 950
Final state:
  Alice USDC: 950
  Alice Yes:  50
  Alice No:   50
```
---
## Tests
| Test file | Coverage |
|-----------|----------|
| `tests/api/test_usdc.py` | `mint_usdc`, `usdc_balance`, `transfer_usdc`, insufficient-balance rejection |
| `tests/api/test_positions.py` | `split_position`, `merge_positions`, insufficient USDC, insufficient tokens |
| `tests/api/test_resolution.py` | `redeem_position` payout mechanics |
| `tests/api/test_lifecycle.py` | Full market lifecycle including cancel refund |
Run:
```bash
pytest -s tests/api/test_usdc.py tests/api/test_positions.py
```
