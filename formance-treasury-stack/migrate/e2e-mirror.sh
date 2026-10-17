#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# v2 -> v3 native migration rehearsal: Ledger v2 at the production version (default v2.2.58) on PostgreSQL, the pinned v3
# node, `ledgers create --mode mirror` (PostgreSQL source, DSN from MIRROR_DSN), verify.py, write barrier, `ledgers promote`.
# Needs: docker, python3, LEDGER_BIN, LEDGERCTL. Usage: LEDGER_BIN=... LEDGERCTL=... migrate/e2e-mirror.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
: "${LEDGER_BIN:?set LEDGER_BIN}" "${LEDGERCTL:?set LEDGERCTL}"
V2_IMAGE="${V2_IMAGE:-ghcr.io/formancehq/ledger:v2.2.58}"
W="$(mktemp -d)"; N="mig$$"; LP=""; pass=0; fail=0
cleanup() {
  local rc=$?; set +e
  [ -n "$LP" ] && kill "$LP" 2>/dev/null && wait "$LP" 2>/dev/null
  docker rm -f "$N-ledger" "$N-pg" >/dev/null 2>&1; docker network rm "$N" >/dev/null 2>&1
  rm -rf "$W"; exit $rc
}
trap cleanup EXIT
ok() { echo "PASS  $1"; pass=$((pass+1)); }
ko() { echo "FAIL  $1 -> $2"; fail=$((fail+1)); }
export LEDGERCTL_INSECURE=true NO_PROXY="localhost,127.0.0.1,${NO_PROXY:-}" no_proxy="localhost,127.0.0.1,${no_proxy:-}"
CTL() { "$LEDGERCTL" --server 127.0.0.1:38888 "$@"; }

docker network create "$N" >/dev/null
docker run -d --name "$N-pg" --network "$N" -p 127.0.0.1:35432:5432 -e POSTGRES_USER=ledger -e POSTGRES_PASSWORD=ledger \
  -e POSTGRES_DB=ledger postgres:16-alpine >/dev/null
for _ in $(seq 90); do docker exec "$N-pg" pg_isready -U ledger >/dev/null 2>&1 && break; sleep 1; done
docker run -d --name "$N-ledger" --network "$N" -p 127.0.0.1:33068:3068 "$V2_IMAGE" serve --auto-upgrade --bind 0.0.0.0:3068 \
  --postgres-uri "postgres://ledger:ledger@$N-pg:5432/ledger?sslmode=disable" >/dev/null
for _ in $(seq 90); do curl -fs 127.0.0.1:33068/_info >/dev/null 2>&1 && break; sleep 1; done
V2V=$(curl -fsS 127.0.0.1:33068/_info | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["version"])')
echo "== source Ledger $V2V"
python3 "$HERE/seed_v2.py" http://127.0.0.1:33068 sovereign-book >/dev/null

echo "== v2 HTTP log contract (the v3 HTTP mirror sends sort=id:asc and query={\"\$gt\":{\"id\":N}})"
first=$(curl -fsS -G 127.0.0.1:33068/v2/sovereign-book/logs --data-urlencode pageSize=3 --data-urlencode sort=id:asc \
  --data-urlencode 'query={"$gt":{"id":5}}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["cursor"]["data"][0]["id"])')
if [ "$first" = 6 ]; then echo "INFO  HTTP source honours the contract: either transport is usable"
else echo "INFO  HTTP source ignores sort/filter (first id after 5 = $first): use the PostgreSQL source"; fi

( cd "$W" && exec "$LEDGER_BIN" run --node-id 1 --cluster-id mig --bootstrap --bind-addr 127.0.0.1:37777 --grpc-port 38888 \
    --http-port 39000 --wal-dir ./wal --data-dir ./data ${LEDGER_EXTRA_FLAGS:-} > ledger.log 2>&1 ) & LP=$!
for _ in $(seq 60); do curl -fs 127.0.0.1:39000/health >/dev/null 2>&1 && break; sleep 0.5; done

echo "== mirror"
MIRROR_DSN="postgres://ledger:ledger@127.0.0.1:35432/ledger?sslmode=disable" CTL ledgers create treasury --mode mirror \
  --mirror-source-type postgres --mirror-ledger-name sovereign-book --index reference --index account-asset >/dev/null \
  && ok "mirror ledger created (DSN from MIRROR_DSN)" || ko "mirror ledger created" "ledgerctl failed"
hwm() { curl -fsS '127.0.0.1:33068/v2/sovereign-book/logs?pageSize=1' | python3 -c 'import sys,json;print(json.load(sys.stdin)["cursor"]["data"][0]["id"])'; }
cursor() { CTL ledgers get treasury 2>&1 | sed -n 's/^ *Cursor: *//p'; }
sync() { local h; h=$(hwm); for _ in $(seq 60); do [ "$(cursor)" = "$h" ] && return 0; sleep 1; done; return 1; }
sync && ok "initial sync complete" || ko "initial sync" "$(CTL ledgers get treasury 2>&1 | grep -E 'State|Remaining')"
VER="V2_URL=http://127.0.0.1:33068 V2_LEDGER=sovereign-book V3_URL=http://127.0.0.1:39000 V3_LEDGER=treasury"
verify() { env $VER EXPECT_REFERENCES=52a041a4-d27d-4612-9366-c962d7b69ef8-EXEC2 python3 "$HERE/verify.py" > "$W/v.json"; }
verify && ok "verification passes after initial sync" || ko "verification after sync" "$(cat "$W/v.json")"

curl -fsS -o /dev/null -X POST 127.0.0.1:33068/v2/sovereign-book/transactions -H 'content-type: application/json' \
  -d '{"postings":[{"source":"world","destination":"treasury:corpus:main","amount":555,"asset":"USD/2"}],"reference":"LIVE-1"}'
sync && verify && ok "live source write followed and verified" || ko "live follow" "$(cat "$W/v.json")"

echo '{"treasury:corpus:main":"treasury:corpus:other"}' > "$W/xw.json"
if CROSSWALK_FILE="$W/xw.json" verify; then ko "negative control" "a wrong crosswalk passed"; else ok "negative control: a wrong crosswalk fails"; fi

code=$(curl -s -o /dev/null -w '%{http_code}' -X POST 127.0.0.1:39000/v3/treasury/transactions -H 'content-type: application/json' \
  -d '{"postings":[{"source":"world","destination":"x","amount":1,"asset":"USD/2"}]}')
[ "$code" = 409 ] && ok "mirror refuses client writes before promotion" || ko "mirror write refusal" "HTTP $code"

echo "== cutover (write barrier = no further source writes; final sync; verify; promote)"
sync && verify && ok "final sync: mirror cursor = source high-water log $(hwm), verified" || ko "final verify" "$(cat "$W/v.json")"
CTL ledgers promote treasury -y >/dev/null && CTL ledgers get treasury 2>&1 | grep -q 'Mode: *NORMAL' \
  && ok "promoted to NORMAL" || ko "promote" "$(CTL ledgers get treasury 2>&1 | grep Mode)"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST 127.0.0.1:39000/v3/treasury/transactions -H 'content-type: application/json' \
  -d '{"postings":[{"source":"world","destination":"treasury:corpus:main","amount":1,"asset":"USD/2"}],"reference":"V3-FIRST"}')
[ "$code" = 201 ] && ok "promoted ledger accepts writes" || ko "post-promotion write" "HTTP $code"

echo; echo "RESULT: $pass passed, $fail failed"; [ $fail -eq 0 ]
