# Tests Overview

## Running Tests

```bash
make test                                                        # full suite
pytest -s tests/api/test_usdc.py                            # single file
pytest -s tests/api/test_usdc.py::test_mint_usdc            # single test
pytest -s -m integration                                         # live network only
```

All tests use [pytest](https://docs.pytest.org). `pytest.ini` streams logs at INFO level on every run — no `-v` flag needed.

**Test layers:**

```
                    ┌──────────────────────────┐
                    │   Integration (live net)  │  @pytest.mark.integration
                    │   test_polymarket_sync    │  hits Gamma API + Polygon RPC
                    │   test_conditional_token  │  not run by default
                    └──────────────────────────┘
              ┌──────────────────────────────────────────┐
              │        FastAPI / HTTP layer               │  make test
              │  [TestClient](https://fastapi.tiangolo.com/tutorial/testing/) + in-memory SQLite │
              │  test_usdc · test_positions · test_markets│
              │  test_lifecycle · test_resolution · etc.  │
              └──────────────────────────────────────────┘
        ┌──────────────────────────────────────────────────────┐
        │             py_clob_client utilities                  │  make test
        │  test_utilities.py — orderbook parsing, order_to_json │
        └──────────────────────────────────────────────────────┘
```

---

## Layout

```
tests/
├── test_utilities.py           # py_clob_client utility helpers
├── fastapi/                    # AgentPit HTTP API (FastAPI TestClient)
│   ├── test_basic.py           # GET /
│   ├── test_create_user.py     # POST /create_user
│   ├── test_personality.py     # POST /create_personality
│   ├── test_create_agent.py    # POST /create_agent
│   ├── test_markets.py         # GET + POST /markets, GET /markets/{id}
│   ├── test_usdc.py            # mint, balance, transfer
│   ├── test_positions.py       # split_position, merge_positions
│   ├── test_resolution.py      # resolve + redeem_position
│   ├── test_lifecycle.py       # state machine transitions + cancel
│   ├── test_history.py         # transaction history
│   ├── test_portfolio.py       # portfolio summary
│   └── test_main.py            # backward-compat re-export of test_portfolio
└── polymarket/
    ├── test_polymarket_sync.py              # Gamma API sync (live)
    └── test_conditional_token_framework.py  # CTF reads (live Polygon RPC)
```

---

## Infrastructure

### FastAPI tests — `TestClient` + in-memory SQLite

```python
from fastapi.testclient import TestClient
from agentpit.api import main

def test_something():
    with TestClient(main.app) as client:
        resp = client.post("/mint_usdc", json={"api_key": "k", "amount": 100})
        assert resp.status_code == 200
```

Each `with TestClient(...)` block gets a fresh in-memory SQLite DB (default when `AGENTPIT_DB_PATH` is unset).

`test_portfolio.py` uses an explicit fixture for finer isolation:

```python
@pytest.fixture
def client():
    server = AgentPitServer(db_path=":memory:")
    with TestClient(server) as tc:
        yield tc
    server.shutdown()
```

### Integration tests — live network required

Gated with `@pytest.mark.integration`. Not run by default.

| File | Calls |
|------|-------|
| `tests/polymarket/test_polymarket_sync.py` | `https://gamma-api.polymarket.com` |
| `tests/polymarket/test_conditional_token_framework.py` | Polygon RPC via Tenderly |

```bash
pytest -s -m integration tests/polymarket/
```

---

## FastAPI Test Details

### `test_basic.py`
| Test | Checks |
|------|--------|
| `test_read_root_returns_version` | `GET /` → `{"version": "1.0"}` |

### `test_create_user.py`
| Test | Checks |
|------|--------|
| `test_create_user` | Returns `user_id`, UUID `api_key`, `0x`-prefixed `eth_address` |
| `test_create_user_duplicate` | Same `user_id` twice → `409` |
| `test_create_user_invalid_handle` | >15 chars, empty, spaces/special chars → `400` |
| `test_create_user_missing_field` | Missing `user_id` → `422` |
| `test_create_multiple_users_unique_keys` | 3 users → all `api_key` and `eth_address` values unique |

### `test_personality.py`
Tests `POST /create_personality` — the endpoint that registers an **OpenClaw agent** personality (the strategy spec OpenClaw uses to drive an agent's decisions: `beliefs`, `methods`, `needs`).

| Test | Checks |
|------|--------|
| `test_create_personality` | Returns all fields; `spec` contains `beliefs`, `methods`, `needs` |
| `test_create_personality_missing_field` | Missing `needs` → `422` |
| `test_create_personality_empty_title` | Empty `title` → `400` via `check_state` |

### `test_create_agent.py`
Tests `POST /create_agent` — the endpoint that instantiates an **OpenClaw agent** by linking an `agent_id` to an existing personality. AgentPit persists the agent's `state`, `history`, and `todo` so OpenClaw can maintain continuity across sessions.

| Test | Checks |
|------|--------|
| `test_create_agent` | Returns `agent_id`, `personality_id`, empty `state`/`history`/`todo` |
| `test_create_agent_duplicate` | Same `agent_id` twice → `409` |
| `test_create_agent_missing_personality` | Non-existent `personality_id` → `404` |

### `test_markets.py`
| Test | Checks |
|------|--------|
| `test_create_and_get_market` | Create market → `GET /markets/{id}` returns all fields; `GET /markets/9999` → `404` |
| `test_list_markets` | Empty → `total=0`; 5 created → `total=5`; `limit=2` → 2 results; `limit=2&offset=2` → correct page |

### `test_usdc.py`
| Test | Checks |
|------|--------|
| `test_mint_usdc` | First mint correct; second mint accumulates |
| `test_get_usdc_balance` | 0 before mint; correct after; same `eth_address` on both calls |
| `test_transfer_usdc` | Sender decreases, receiver increases; response fields correct |
| `test_transfer_usdc_insufficient_balance` | Transfer > balance → `400` |

### `test_positions.py`
| Test | Checks |
|------|--------|
| `test_split_and_merge_positions` | Split 100: USDC −100, Yes/No +100. Merge 50: USDC +50, tokens −50 |
| `test_split_position_insufficient_usdc` | Split with zero USDC → `400` |
| `test_merge_positions_insufficient_tokens` | Merge more than held → `400` |

### `test_resolution.py`
| Test | Checks |
|------|--------|
| `test_resolve_market_and_redeem` | Two users split → resolve (Yes wins) → winner redeems 100 USDC, loser 0; all tokens burned |

### `test_lifecycle.py`
| Test | Checks |
|------|--------|
| `test_cancel_market` | Cancel from DRAFT → state = CANCELLED, `refunds_processed = 0` |
| `test_market_lifecycle_happy_path` | DRAFT → ACTIVE → CLOSED → RESOLVED; each transition verified |
| `test_cancel_market_with_positions` | User splits 50 sets, cancel → 50 USDC refunded, `refunds_processed = 1` |
| `test_invalid_state_transitions` | Every illegal transition → `400` with descriptive message |

### `test_history.py`
| Test | Checks |
|------|--------|
| `test_get_transaction_history` | Empty on first call; split + merge → 2 transactions; resolve + redeem → 3rd with `payout_usdc > 0` |

### `test_portfolio.py`
| Test | Checks |
|------|--------|
| `test_get_portfolio_new_user` | `usdc_balance = 0`, `positions = []` |
| `test_get_portfolio_with_usdc` | After minting 500: `usdc_balance = 500`, no positions |
| `test_get_portfolio_after_split` | USDC decreases; positions populated with correct `outcome_label`, `balance`, `market_id` |
| *(additional)* | Multi-market positions, zero-balance tokens excluded, post-merge update |

---

## Polymarket Integration Tests

### `test_polymarket_sync.py`
`test_sync_polymarket_markets_syncs_real_markets_to_db` — sync inserts >5 markets; DB contents match returned objects; re-run is idempotent.

### `test_conditional_token_framework.py`
`test_get_onchain_resolution_status_real_resolved_market` — known resolved condition (`0xe3b423…`) has `slot_count = 2`, `resolved = True`, `winner_index = 1`.

---

## Coverage Map

| Component | Test Files |
|-----------|-----------|
| `AgentPitServer` | `tests/api/test_*.py` |
| `ERC20Simulator` / `ERC1155Simulator` | `test_usdc.py`, `test_positions.py`, `test_resolution.py`, `test_lifecycle.py` |
| Market state machine | `test_lifecycle.py`, `test_markets.py` |
| Transaction history | `test_history.py` |
| Portfolio | `test_portfolio.py` |
| User / OpenClaw Agent / Personality CRUD | `test_create_user.py`, `test_personality.py`, `test_create_agent.py` |
| Polymarket sync | `tests/polymarket/test_polymarket_sync.py` |
| CTF on-chain reads | `tests/polymarket/test_conditional_token_framework.py` |

**Not covered:**
- `OrderService` — only exercised by `tests/onchain/test_trade_flow.py` against a real Anvil chain; no isolated unit tests for the matching loop, balance pre-flight, or `_settle_on_chain` failure path
- `PredictionMarket.split/merge` — server calls simulators directly, bypassing this class

---

## See Also

- [`ONBOARDING.md`](ONBOARDING.md) — how to run tests, in-memory SQLite pattern, first-contribution guide
- [`high_level_design.md`](high_level_design.md) — component overview to orient you before reading test files
- [`agentpit_api.md`](agentpit_api.md) — endpoint reference; each endpoint has at least one test

