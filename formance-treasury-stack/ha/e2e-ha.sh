#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Three-voter Ledger v3 cluster: node 1 --bootstrap, nodes 2-3 --join (auto-promoted learners). Kills the leader under
# write load, proves writes continue on the surviving majority, restarts the node and proves catch-up and identical state.
# Needs: LEDGER_BIN, LEDGERCTL. Usage: LEDGER_BIN=... LEDGERCTL=... ha/e2e-ha.sh
set -euo pipefail
: "${LEDGER_BIN:?set LEDGER_BIN}" "${LEDGERCTL:?set LEDGERCTL}"
W="$(mktemp -d)"; declare -A PID; pass=0; fail=0
export LEDGERCTL_INSECURE=true NO_PROXY="localhost,127.0.0.1,${NO_PROXY:-}" no_proxy="localhost,127.0.0.1,${no_proxy:-}"
cleanup() { local rc=$?; set +e; for n in 1 2 3; do [ -n "${PID[$n]:-}" ] && kill "${PID[$n]}" 2>/dev/null; done; wait 2>/dev/null; rm -rf "$W"; exit $rc; }
trap cleanup EXIT
ok() { echo "PASS  $1"; pass=$((pass+1)); }
ko() { echo "FAIL  $1 -> $2"; fail=$((fail+1)); }
raft() { echo $((47770 + $1)); }; grpc() { echo $((48880 + $1)); }; http() { echo $((49000 + $1)); }
start() {   # $1 node id, rest: --bootstrap