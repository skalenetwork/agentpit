#!/usr/bin/env bash
# Deploy the full agentpit stack from scratch to the chain at RPC_URL:
#   ConditionalTokens (CTF) + AgentpitUSD + Faucet + CTFExchange.
# Usage: ./scripts/deploy_exchange.sh
#
# In development RPC_URL is the node from run_node.sh; in production it is
# SKALE on Base, and this runs as deploy/'s chain-init service. Nothing is
# inherited from Polygon — the CTF is deployed here from a committed artifact.
# Writes all resulting addresses to agentpit/deployments/local.json for the
# Python side.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENTPIT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$AGENTPIT_DIR/.env"
CTF_EXCHANGE_DIR="${CTF_EXCHANGE_DIR:-$AGENTPIT_DIR/vendor/ctf-exchange}"

# Faucet drip amount = the USER signup grant: $100,000 apUSD (6 decimals).
# The house is funded separately by Faucet.mintTo, so this figure no longer has
# to serve both.
SIGNUP_GRANT_RAW="${SIGNUP_GRANT_RAW:-100000000000}"

if [ ! -f "$ENV_FILE" ]; then
  echo "Error: $ENV_FILE not found. Copy .env.example and edit." >&2
  exit 1
fi

# shellcheck disable=SC1090
source "$ENV_FILE"

# EOA-only signatures → the proxy/safe factories are never invoked. Force them
# to 0x0 AFTER sourcing .env so any stale PROXY_FACTORY/SAFE_FACTORY left in the
# env file cannot leak into the deploy or local.json.
ZERO_ADDR="0x0000000000000000000000000000000000000000"
PROXY_FACTORY="$ZERO_ADDR"
SAFE_FACTORY="$ZERO_ADDR"

for var in PK ADMIN RPC_URL; do
  if [ -z "${!var:-}" ]; then
    echo "Error: $var is not set in $ENV_FILE" >&2
    exit 1
  fi
done

for bin in forge cast jq; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "Error: $bin not found." >&2
    exit 1
  fi
done

# --- Ensure the vendored contract submodule (+ its 4 libs) is checked out -----
# Only auto-init when using the default vendored path; an overridden
# CTF_EXCHANGE_DIR is the caller's responsibility.
if [ "$CTF_EXCHANGE_DIR" = "$AGENTPIT_DIR/vendor/ctf-exchange" ]; then
  if [ ! -f "$CTF_EXCHANGE_DIR/foundry.toml" ]; then
    echo "Initializing vendor/ctf-exchange submodule..."
    git -C "$AGENTPIT_DIR" submodule update --init vendor/ctf-exchange
  fi
  # Presence is not enough: `git pull` on the superproject moves the gitlink but
  # leaves the submodule working tree on its old commit, and `submodule.recurse`
  # is not set. Deploying that mismatch compiles the OLD contracts against the
  # NEW checked-in ABIs -- the failure surfaces only after the chain and the
  # database have already been wiped. Compare and correct.
  WANT_SHA="$(git -C "$AGENTPIT_DIR" rev-parse HEAD:vendor/ctf-exchange 2>/dev/null || true)"
  HAVE_SHA="$(git -C "$CTF_EXCHANGE_DIR" rev-parse HEAD 2>/dev/null || true)"
  if [ -n "$WANT_SHA" ] && [ "$WANT_SHA" != "$HAVE_SHA" ]; then
    echo "vendor/ctf-exchange is at ${HAVE_SHA:0:12}, superproject expects ${WANT_SHA:0:12} -- updating..."
    git -C "$AGENTPIT_DIR" submodule update --init vendor/ctf-exchange
    HAVE_SHA="$(git -C "$CTF_EXCHANGE_DIR" rev-parse HEAD 2>/dev/null || true)"
    if [ "$WANT_SHA" != "$HAVE_SHA" ]; then
      echo "Error: could not check out $WANT_SHA in $CTF_EXCHANGE_DIR." >&2
      echo "       Fetch it in the submodule first; deploying the wrong contracts" >&2
      echo "       is only discoverable after the chain is already destroyed." >&2
      exit 1
    fi
  fi
  for lib in forge-std openzeppelin-contracts solmate solady; do
    if [ -z "$(ls -A "$CTF_EXCHANGE_DIR/lib/$lib" 2>/dev/null)" ]; then
      git -C "$CTF_EXCHANGE_DIR" submodule update --init "lib/$lib"
    fi
  done
fi

if [ ! -d "$CTF_EXCHANGE_DIR" ]; then
  echo "Error: ctf-exchange not found at $CTF_EXCHANGE_DIR" >&2
  exit 1
fi

DEPLOYER="$(cast wallet address --private-key "$PK")"

echo "Deploying agentpit stack from scratch to $RPC_URL"
echo "  deployer:      $DEPLOYER"
echo "  admin:         $ADMIN"
echo "  proxy_factory: $PROXY_FACTORY (stub)"
echo "  safe_factory:  $SAFE_FACTORY (stub)"
echo "  signup grant:  $SIGNUP_GRANT_RAW raw apUSD"
echo

# Fund the deployer with 10,000 native (0x21E19E0C9BAB2400000 == 10_000 * 1e18).
# Only a local anvil has that cheat. On a real chain -- SKALE, a testnet -- the
# method does not exist and the node answers -32601, which used to kill the
# deploy on its first RPC call. There the deployer is funded out of band, so a
# missing cheat is not an error; an empty wallet still is, and it is better to
# say so here than to fail three contracts later.
if ! cast rpc anvil_setBalance "$DEPLOYER" 0x21E19E0C9BAB2400000 \
     --rpc-url "$RPC_URL" >/dev/null 2>&1; then
  BAL="$(cast balance "$DEPLOYER" --rpc-url "$RPC_URL")"
  if [ "$BAL" = "0" ]; then
    echo "Error: no anvil_setBalance on $RPC_URL and $DEPLOYER holds no gas." >&2
    exit 1
  fi
  echo "  funding:       external, $BAL wei already held"
fi

# --legacy pays eth_gasPrice and nothing more. SKALE gas is not free: mainnet
# charged about 47.6 gwei of CREDIT on 2026-10-14. skaled also bills a type-2
# transaction its whole maxFeePerGas rather than base fee + tip, and foundry,
# like web3, sets that to about twice the base fee, so a type-2 deploy would
# pay double. (An older skaled answered eth_feeHistory with -32601, which is
# what first brought this flag here.) anvil accepts legacy transactions too, so
# this is safe to pass everywhere rather than branching on the chain.
GAS_MODE="--legacy"
# --slow sends each forge script transaction only once the previous one has a
# receipt, instead of all of them back to back. The 2026-10-12 mainnet deploy
# ran this way. Even then forge reported a transaction as discarded that had in
# fact landed, so judge a deploy by the chain (`cast code <address>`), not by
# forge's summary. On anvil every transaction mines at once, so it costs
# nothing there.
FORGE_SLOW="--slow"

# --- 1. Deploy ConditionalTokens from its committed creation bytecode ---------
CTF_ARTIFACT="$CTF_EXCHANGE_DIR/artifacts/ConditionalTokens.json"
CTF_BYTECODE="$(jq -r 'if (.bytecode | type) == "object" then .bytecode.object else .bytecode end' "$CTF_ARTIFACT")"
if [ -z "$CTF_BYTECODE" ] || [ "$CTF_BYTECODE" = "null" ]; then
  echo "Error: could not read bytecode from $CTF_ARTIFACT" >&2
  exit 1
fi

CTF="$(cast send \
  --private-key "$PK" \
  --rpc-url "$RPC_URL" \
  $GAS_MODE \
  --json \
  --create "$CTF_BYTECODE" | jq -r .contractAddress)"

if [ -z "$CTF" ] || [ "$CTF" = "null" ]; then
  echo "Error: ConditionalTokens deploy failed (no contractAddress)." >&2
  exit 1
fi
# cast returns a lowercase (non-EIP-55) address; checksum it so every address in
# local.json is uniformly EIP-55, matching the forge-returned usd/faucet/exchange.
# (web3.py rejects a non-checksummed address wherever deployment.ctf is used raw
# as a call argument — e.g. allowance(_, deployment.ctf) in the onchain tests.)
CTF="$(cast to-check-sum-address "$CTF")"
echo "ConditionalTokens deployed: $CTF"

# --- 2. Deploy AgentpitUSD + Faucet + CTFExchange via the forge script --------
OUTPUT="$(cd "$CTF_EXCHANGE_DIR" && forge script ExchangeDeployment \
    --private-key "$PK" \
    --rpc-url "$RPC_URL" \
    $GAS_MODE \
    $FORGE_SLOW \
    --json \
    --broadcast \
    -s "deployAgentpitStack(address,address,address,address,uint256)" \
    "$ADMIN" "$CTF" "$PROXY_FACTORY" "$SAFE_FACTORY" "$SIGNUP_GRANT_RAW")"

RETURNS_JSON="$(echo "$OUTPUT" | grep -E '^\{' | jq -c 'select(.returns)' 2>/dev/null | head -n1 || true)"
if [ -z "$RETURNS_JSON" ]; then
  echo "Failed to parse forge output." >&2
  echo "$OUTPUT" >&2
  exit 1
fi

USD="$(echo "$RETURNS_JSON" | jq -r '.returns.usd.value')"
FAUCET="$(echo "$RETURNS_JSON" | jq -r '.returns.faucet.value')"
EXCHANGE="$(echo "$RETURNS_JSON" | jq -r '.returns.exchange.value')"

for var in USD FAUCET EXCHANGE; do
  if [ -z "${!var:-}" ] || [ "${!var:-}" = "null" ]; then
    echo "Failed to extract $var from forge output." >&2
    echo "$OUTPUT" >&2
    exit 1
  fi
done

echo "AgentpitUSD deployed: $USD"
echo "Faucet      deployed: $FAUCET"
echo "Exchange    deployed: $EXCHANGE"

# Ask the chain rather than assume anvil's 31337. This number is the EIP-712
# domain separator the exchange validates every order signature against, so a
# wrong one does not fail loudly at deploy -- it deploys a stack where every
# single order is rejected as a bad signature, on a chain that looks healthy.
CHAIN_ID="$(cast chain-id --rpc-url "$RPC_URL")"
echo "Chain id:       $CHAIN_ID"

mkdir -p "$AGENTPIT_DIR/deployments"
cat > "$AGENTPIT_DIR/deployments/local.json" <<EOF
{
  "chain_id": $CHAIN_ID,
  "rpc_url": "$RPC_URL",
  "admin": "$ADMIN",
  "usd": "$USD",
  "faucet": "$FAUCET",
  "ctf": "$CTF",
  "proxy_factory": "$PROXY_FACTORY",
  "safe_factory": "$SAFE_FACTORY",
  "exchange": "$EXCHANGE",
  "signup_grant_raw": "$SIGNUP_GRANT_RAW"
}
EOF

echo "Wrote $AGENTPIT_DIR/deployments/local.json"
