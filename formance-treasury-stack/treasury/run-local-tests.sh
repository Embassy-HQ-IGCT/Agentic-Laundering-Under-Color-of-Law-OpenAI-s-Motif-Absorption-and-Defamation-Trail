#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Full local verification: single-node Formance Ledger v3 + the treasury engine, then
#   1. functional end-to-end suite   2. resilience/security suite   3. graceful shutdown   4. production preflight refusals
# Needs: a Ledger v3 server binary and ledgerctl (clone formancehq/ledger; go build -o ledger-bin . ; go build -o ledgerctl ./cmd/ledgerctl), python3 >= 3.9.
# Usage: LEDGER_BIN=... LEDGERCTL=... [PG_URL=postgresql://u:p@host:5432/db] [RAFIKI_WEBHOOKS=recorded.jsonl] [LEDGER_EXTRA_FLAGS=...] ./run-local-tests.sh
# PG_URL runs the SAME suites against PostgreSQL (needs `pip install -r requirements.txt`); default is SQLite.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
: "${LEDGER_BIN:?set LEDGER_BIN}" "${LEDGERCTL:?set LEDGERCTL}"
W="$(mktemp -d)"; LP=""; EP=""
cleanup() {
  local rc=$?; set +e                          # preserve the script's real exit status; cleanup must never change it
  [ -n "$EP" ] && kill $EP 2>/dev/null
  [ -n "$LP" ] && kill $LP 2>/dev/null
  for _ in $(seq 40); do { [ -z "$LP" ] || ! kill -0 $LP 2>/dev/null; } && break; sleep 0.25; done   # wait for the Ledger to actually exit
  [ -n "$LP" ] && kill -9 $LP 2>/dev/null
  rm -rf "$W"
  exit $rc
}
trap cleanup EXIT
export LEDGERCTL_INSECURE=true

TEST_SECRET="test-secret-0123456789-0123456789-abcdef"; RAFIKI_SECRET="rafiki-secret-0123456789-0123456789-abcdef"
KEYS="{\"test\":{\"secret\":\"$TEST_SECRET\",\"scopes\":[\"*\"],\"rps\":5000},\"cardonly\":{\"secret\":\"cardonly-secret-0123456789-0123456789-ab\",\"scopes\":[\"card\"],\"rps\":5000},\"slow\":{\"secret\":\"slow-secret-0123456789-0123456789-abcdefg\",\"scopes\":[\"read\"],\"rps\":2}}"

( cd "$W" && exec "$LEDGER_BIN" run --node-id 1 --cluster-id local --bootstrap --bind-addr 127.0.0.1:17777 --grpc-port 18888 --http-port 19000 \
    --wal-dir ./wal --data-dir ./data ${LEDGER_EXTRA_FLAGS:-} > ledger.log 2>&1 ) & LP=$!
for _ in $(seq 60); do curl -fs localhost:19000/health >/dev/null 2>&1 && break; sleep 0.5; done

# Ledger bootstrap (also what the production init Job does): create the ledger, then the REQUIRED reference index.
LN=treasury
curl -fsS -X POST "localhost:19000/v3/$LN" -H 'content-type: application/json' -d '{}' >/dev/null
"$LEDGERCTL" --server localhost:18888 indexes create --ledger "$LN" --type reference >/dev/null
for _ in $(seq 60); do
  curl -fs -G "localhost:19000/v3/$LN/transactions" --data-urlencode 'pageSize=1' --data-urlencode 'filter={"$match":{"reference":"probe"}}' >/dev/null 2>&1 && break
  sleep 0.5
done

# HARDENED_RUN=1 : run the engine as unprivileged uid 10001 from a READ-ONLY copy of the sources (what the container does).
RUNAS=""; SRC="$HERE"
if [ -n "${HARDENED_RUN:-}" ]; then
  chmod a+rx "$W"; SRC="$W/ro"; mkdir -p "$SRC"; cp "$HERE"/*.py "$SRC/"; chmod -R a-w "$SRC"; chmod a+rx "$SRC"
  RUNAS="setpriv --reuid=10001 --regid=10001 --clear-groups"
fi
start_engine() {   # $1 = extra env assignments (space separated) ; sets EP
  env PYTHONDONTWRITEBYTECODE=1 HOME=/nonexistent TREASURY_ENV=test TREASURY_ALLOW_MOCK_BANKS=1 TREASURY_BG=0 TREASURY_API_KEYS="$KEYS" RAFIKI_WEBHOOK_SECRET="$RAFIKI_SECRET" \
      LEDGER_URL=http://127.0.0.1:19000 LEDGER_NAME=$LN PORT=18200 METRICS_PORT=19100 \
      DATABASE_URL="${PG_URL:-sqlite:///$W/treasury.db}" $1 $RUNAS python3 -I "$SRC/server.py" > "$W/engine.log" 2>&1 &
  EP=$!
  for _ in $(seq 60); do curl -fs localhost:18200/livez >/dev/null 2>&1 && break; sleep 0.25; done
}
start_engine ""
export TEST_KEY_SECRET="$TEST_SECRET" RAFIKI_WEBHOOK_SECRET="$RAFIKI_SECRET" METRICS_PORT=19100
RC=0
[ -n "${HARDENED_RUN:-}" ] && echo "(hardened run: engine uid=$(ps -o uid= -p $EP | tr -d ' '), source tree read-only)"
echo "################ functional suite (db: ${PG_URL:+postgres}${PG_URL:-sqlite})"
python3 "$HERE/tests/e2e.py" 127.0.0.1:18200 "${RAFIKI_WEBHOOKS:-}" || RC=1
echo "################ resilience + security suite"
python3 "$HERE/tests/resilience.py" 127.0.0.1:18200 "${RAFIKI_WEBHOOKS:-}" || RC=1

echo "################ operator-assisted bank (manual channel): instruction out, CSV/BAI2 statement in"
MP=""
curl -fsS -X POST "localhost:19000/v3/manualflow" -H 'content-type: application/json' -d '{}' >/dev/null
"$LEDGERCTL" --server localhost:18888 indexes create --ledger manualflow --type reference >/dev/null
for _ in $(seq 60); do
  curl -fs -G "localhost:19000/v3/manualflow/transactions" --data-urlencode 'pageSize=1' --data-urlencode 'filter={"$match":{"reference":"probe"}}' >/dev/null 2>&1 && break
  sleep 0.5
done
env PYTHONDONTWRITEBYTECODE=1 HOME=/nonexistent TREASURY_ENV=test TREASURY_BG=0 TREASURY_API_KEYS="$KEYS" RAFIKI_WEBHOOK_SECRET="$RAFIKI_SECRET" \
    LEDGER_URL=http://127.0.0.1:19000 LEDGER_NAME=manualflow PORT=18210 METRICS_PORT=19110 \
    BANKS_JSON='[{"name":"bofa","type":"manual","rails":["FEDWIRE","SWIFT"],"ccys":["USD"]}]' \
    DATABASE_URL="${PG_URL:-sqlite:///$W/manual.db}" python3 -I "$SRC/server.py" > "$W/engine-manual.log" 2>&1 &
MP=$!
for _ in $(seq 60); do curl -fs localhost:18210/livez >/dev/null 2>&1 && break; sleep 0.25; done
python3 "$HERE/tests/manual_flow.py" 127.0.0.1:18210 || RC=1
kill -TERM $MP 2>/dev/null; wait $MP 2>/dev/null

echo "################ graceful shutdown"
kill -TERM $EP
for _ in $(seq 40); do kill -0 $EP 2>/dev/null || break; sleep 0.25; done
if kill -0 $EP 2>/dev/null; then echo "FAIL  engine did not exit within 10 s of SIGTERM"; RC=1; kill -9 $EP
else wait $EP && echo "PASS  engine drained and exited cleanly on SIGTERM" || { echo "FAIL  engine exited non-zero"; RC=1; }; fi
grep -q '"msg": "stopped"' "$W/engine.log" && echo "PASS  shutdown logged" || { echo "FAIL  no 'stopped' log line"; RC=1; }
EP=""

echo "################ production preflight refuses unsafe configurations"
prod() {   # $1 = description ; rest = env assignments ; expects a refusal (non-zero exit with 'refusing to start')
  local d="$1"; shift
  if out=$(env -i PATH="$PATH" TREASURY_ENV=prod LEDGER_URL=http://127.0.0.1:19000 LEDGER_NAME=$LN PORT=18201 METRICS_PORT=19101 "$@" \
        timeout 10 python3 -I "$HERE/server.py" 2>&1); then echo "FAIL  $d: started"; RC=1
  elif echo "$out" | grep -q "refusing to start"; then echo "PASS  $d"; else echo "FAIL  $d: failed for another reason: $(echo "$out" | tail -1 | cut -c1-120)"; RC=1; fi
}
GOODPG="postgresql://x:y@127.0.0.1:1/z"
prod "no API keys"                                   TREASURY_API_KEYS= DATABASE_URL=$GOODPG
prod "SQLite state"                                  TREASURY_API_KEYS="$KEYS" DATABASE_URL=sqlite:///x.db
prod "test clock enabled"                            TREASURY_API_KEYS="$KEYS" DATABASE_URL=$GOODPG TREASURY_TEST_CLOCK=1
prod "admin endpoints enabled"                       TREASURY_API_KEYS="$KEYS" DATABASE_URL=$GOODPG TREASURY_ADMIN=1
prod "simulated (mock) banks"                        TREASURY_API_KEYS="$KEYS" DATABASE_URL=$GOODPG
prod "weak API key secret"                           TREASURY_API_KEYS='{"k":{"secret":"short","scopes":["*"]}}' DATABASE_URL=$GOODPG
[ $RC -eq 0 ] && echo && echo "ALL SUITES PASSED" || { echo; echo "SOME CHECKS FAILED"; }
exit $RC
