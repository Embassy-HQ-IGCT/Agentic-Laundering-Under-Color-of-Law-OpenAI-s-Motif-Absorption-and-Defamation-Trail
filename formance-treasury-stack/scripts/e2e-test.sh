#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# End-to-end checks for the Formance CE install. Run from the install dir (e.g. formance/clean).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"; cd "${1:-$PWD}" || exit 1
P=localhost:8080; pass=0; fail=0
ok(){ echo "PASS  $1"; pass=$((pass+1)); }; ko(){ echo "FAIL  $1  -> $2"; fail=$((fail+1)); }
check(){ if [ "$2" = "$3" ]; then ok "$1"; else ko "$1" "expected [$3] got [$2]"; fi; }
export LEDGERCTL_INSECURE=true

echo "== Infra"
check "payments server health"   "$(curl -s -o /dev/null -w '%{http_code}' $P/_healthcheck)" 200
check "payments worker health"   "$(curl -s -o /dev/null -w '%{http_code}' localhost:8082/_healthcheck)" 200
check "temporal UI up"           "$(curl -s -o /dev/null -w '%{http_code}' localhost:8081)" 200
check "postgres healthy"         "$(docker inspect -f '{{.State.Health.Status}}' payments-postgres-1)" healthy
n=$(curl -s $P/v3/connectors/configs | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["data"]))'); check "16 CE connectors exposed" "$n" 16
curl -s $P/v3/connectors/configs | python3 -c 'import sys,json;d=json.load(sys.stdin)["data"];sys.exit(0 if not set(d)&{"bitstamp","fireblocks","krakenpro","routable","coinbaseprime","bankingbridge"} else 1)' && ok "no Enterprise connectors present" || ko "no Enterprise connectors present" "ee connector exposed"

echo "== Ledger v3"
L="./ledgerctl"; [ -x ./ledgerctl ] || L="$HERE/ledgerctl"
name="e2e$(date +%s)"
$L ledgers create $name >/dev/null 2>&1 && ok "create ledger" || ko "create ledger" "failed"
$L tx create --ledger $name --posting "world,bank,1000,USD" >/dev/null 2>&1 && ok "tx world->bank" || ko "tx world->bank" "failed"
$L tx create --ledger $name --posting "bank,user,400,USD" >/dev/null 2>&1 && ok "tx bank->user" || ko "tx bank->user" "failed"
out=$($L accounts list --ledger $name 2>&1)
echo "$out" | grep -q "bank .*USD 600" && echo "$out" | grep -q "user .*USD 400" && ok "balances bank=600 user=400" || ko "balances" "$out"
$L tx create --ledger $name --posting "bank,user,99999,USD" >/dev/null 2>&1 && ko "overdraft rejected" "overdraft accepted" || ok "overdraft rejected"

echo "== Payments connector via Temporal workflow (mock PSP)"
python3 "$HERE/mock_psp.py" >"$HERE/mock_psp.log" 2>&1 & MOCK=$!; sleep 1
r=$(curl -s -X POST $P/v3/connectors/install/generic -H 'content-type: application/json' \
   -d '{"name":"mockpsp","apiKey":"test-key","endpoint":"http://127.0.0.1:9100","pollingPeriod":"20m"}')
cid=$(echo "$r" | python3 -c 'import sys,json;d=json.load(sys.stdin);print(d.get("data") or d)'); [ -n "$cid" ] && ok "install generic connector ($cid)" || ko "install connector" "$r"
for _ in $(seq 40); do cnt=$(curl -s $P/v3/accounts | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["cursor"]["data"]))'); [ "$cnt" -ge 2 ] && break; sleep 2; done
check "2 accounts ingested"  "$cnt" 2
for _ in $(seq 20); do pc=$(curl -s $P/v3/payments | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["cursor"]["data"]))'); [ "$pc" -ge 3 ] && break; sleep 2; done
check "3 payments ingested"  "$pc" 3
bal=$(curl -s $P/v3/accounts | python3 -c '
import sys,json
d=json.load(sys.stdin)["cursor"]["data"]
print(",".join(sorted(a["name"] for a in d)))'); check "account names" "$bal" "Operating,Reserve"
opid=$(curl -s $P/v3/accounts | python3 -c 'import sys,json;print([a["id"] for a in json.load(sys.stdin)["cursor"]["data"] if a["name"]=="Operating"][0])')
b=$(curl -s "$P/v3/accounts/$opid/balances"); echo "$b" | grep -q 6500 && ok "Operating balance 6500 synced" || ko "balance synced" "$b"
wf=$(docker exec payments-temporal-1 temporal workflow list --address "$(docker exec payments-temporal-1 hostname -i):7233" 2>&1 | grep -c -i -E "Completed|Running"); [ "$wf" -ge 1 ] && ok "temporal recorded workflows ($wf)" || ko "temporal workflows" "$wf"
curl -s -X DELETE $P/v3/connectors/$cid >/dev/null; kill $MOCK 2>/dev/null
echo; echo "RESULT: $pass passed, $fail failed"; [ $fail -eq 0 ]
