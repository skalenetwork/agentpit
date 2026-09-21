#!/usr/bin/env bash
# Run a local anvil node — a clean chain with NO Polygon fork.
# Usage: ./scripts/run_node.sh
#
# The node listens on 127.0.0.1:8545 with chain id 31337 (anvil's default).
# The chain id is not load-bearing: EIP-712 order signing reads it from
# deployments/local.json (order_signer uses deployment.chain_id) and the web3
# client verifies node == local.json, so any value works as long as the node,
# local.json, and signing agree. 31337 is chosen because the vendored
# ctf-exchange .gitignore already excludes broadcast/*/31337/, so forge's
# per-deploy broadcast logs stay out of git. Every contract is deployed from
# scratch by scripts/deploy_exchange.sh — nothing is inherited from Polygon.

set -euo pipefail

if ! command -v anvil >/dev/null 2>&1; then
  echo "Error: anvil not found. Install Foundry: https://book.getfoundry.sh/getting-started/installation" >&2
  exit 1
fi

# Anvil keeps the last few hundred block states in memory and writes every
# older one to ~/.foundry/anvil/tmp/anvil-state-<start time>/, one file per
# block, and never deletes that directory — not on exit, not on the next start.
# Measured here: 1,500 blocks left 2.9 GB behind, and a single test-suite run
# 3,599 files / 6 GB. Every restart abandons another directory, so the disk
# fills in days. Remove what earlier runs left, unless an anvil started
# without --prune-history is still running and may be writing into one of them.
ANVIL_TMP="${HOME}/.foundry/anvil/tmp"

unpruned_anvil_running() {
  for pid in $(pgrep -x anvil 2>/dev/null); do
    ps -o command= -p "$pid" | grep -q -- '--prune-history' || return 0
  done
  return 1
}

if [ -d "$ANVIL_TMP" ] && ls -d "$ANVIL_TMP"/anvil-state-* >/dev/null 2>&1; then
  if unpruned_anvil_running; then
    echo "Leaving $ANVIL_TMP alone: an anvil without --prune-history is running"
  else
    freed="$(du -sh "$ANVIL_TMP" 2>/dev/null | cut -f1)"
    rm -rf "$ANVIL_TMP"/anvil-state-*
    echo "Removed state history left by earlier anvil runs ($freed)"
  fi
fi

echo "Starting clean local node on 127.0.0.1:8545 (chain id 31337, no fork)"

# --state persists the full chain to disk (load on start, dump every 30s + on
# exit) so a crash/restart recovers instead of losing every contract + balance.
# Missing file on first run -> starts fresh and creates it.
#
# --prune-history 32 stops the history above from being written at all: anvil
# keeps the latest 32 block states in memory and nothing on disk. Nothing here
# reads state at an old block — every call is against "latest", and neither
# evm_snapshot nor evm_revert is used — so the only cost is memory, measured
# at ~3.4 MB a state (about 110 MB, fixed) instead of gigabytes of files.
exec anvil \
  --host 127.0.0.1 \
  --port 8545 \
  --chain-id 31337 \
  --state /Users/yavorsky/dev/agentpit/.anvil-state.json \
  --state-interval 30 \
  --prune-history 32
