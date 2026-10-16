#!/usr/bin/env bash
# Start a single-node Formance Ledger v3 + the treasury engine (simulated banks) and run the end-to-end suite.
# Needs: a Ledger v3 server binary (build: git clone https://github.com/formancehq/ledger && go build -o ledger-bin .), python3 >= 3.9.
# Usage: LEDGER_BIN=/path/to/ledger-bin [RAFIKI_WEBHOOKS=/path/to/recorded.jsonl] ./run-local-tests.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; : "${LEDGER_BIN:?set LEDGER_BIN to a Ledger v3 binary}"
W="$(mktemp -d)"; trap 'kill $LP $EP 2>/dev/null || true; rm -rf "$W"' EXIT
( cd "$W" && "$LEDGER_BIN" run --node-id 1 --cluster-id local --bootstrap --bind-addr 127.0.0.1:17777 --grpc-port 18888 --http-port 19000 \
    --wal-dir ./wal --data-dir ./data ${LEDGER_EXTRA_FLAGS:-} > ledger.log 2>&1 ) & LP=$!
for _ in $(seq 40); do curl -fs localhost:19000/health >/dev/null 2>&1 && break; sleep 0.5; done
LEDGER_URL=http://127.0.0.1:19000 LEDGER_NAME=treasury TREASURY_ALLOW_MOCK_BANKS=1 TREASURY_TEST_CLOCK=1 TREASURY_ADMIN=1 PORT=18200 \
  python3 -I "$HERE/server.py" > "$W/engine.log" 2>&1 & EP=$!
for _ in $(seq 40); do curl -fs localhost:18200/health >/dev/null 2>&1 && break; sleep 0.5; done
python3 "$HERE/tests/e2e.py" localhost:18200 "${RAFIKI_WEBHOOKS:-}"
