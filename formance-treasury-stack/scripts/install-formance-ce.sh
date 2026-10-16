#!/usr/bin/env bash
# Formance Community Edition: Ledger v3 + Payments (16 CE connectors) + Temporal workflows.
# Native Go binaries; Postgres and Temporal run in containers. Requires: git, go >= 1.27, docker.
set -euo pipefail
WORK="${1:-$PWD/formance}"; mkdir -p "$WORK"; cd "$WORK"

[ -d ledger ]   || git clone --depth 1 https://github.com/formancehq/ledger.git
[ -d payments ] || git clone --depth 1 https://github.com/formancehq/payments.git

# Ledger v3 (single node, Raft; no external DB needed)
(cd ledger && go build -o ../ledger-bin . && go build -o ../ledgerctl ./cmd/ledgerctl)
mkdir -p run-ledger && (cd run-ledger && setsid nohup ../ledger-bin run --node-id 1 --cluster-id local \
  --bootstrap --bind-addr 127.0.0.1:7777 --grpc-port 8888 --http-port 9000 --wal-dir ./wal --data-dir ./data ${LEDGER_EXTRA_FLAGS:-} \
  > ledger.log 2>&1 < /dev/null &)

# Payments: community build (ce/ plugins only; ee/ is Enterprise-licensed)
(cd payments && bash tools/compile-plugins/compile-plugin.sh >/dev/null \
  && go build -ldflags "-X github.com/formancehq/payments/cmd.Edition=community" -o ../payments-ce .)

# Backing services (skip ghcr.io gateway/console; Postgres + Temporal come from Docker Hub)
if ! docker info >/dev/null 2>&1; then
  echo "docker daemon not running; starting dockerd"
  setsid nohup dockerd > dockerd.log 2>&1 < /dev/null &
  for _ in $(seq 30); do docker info >/dev/null 2>&1 && break; sleep 1; done
  docker info >/dev/null 2>&1 || { echo "docker daemon unavailable" >&2; exit 1; }
fi
(cd payments && docker compose up -d postgres temporal temporal-ui)
for _ in $(seq 90); do (exec 3<>/dev/tcp/127.0.0.1/7233) 2>/dev/null && break; sleep 1; done
sleep 10  # frontend needs a moment after the port opens

export POSTGRES_URI='postgres://payments:payments@localhost:5432/payments?sslmode=disable'
export CONFIG_ENCRYPTION_KEY="${CONFIG_ENCRYPTION_KEY:?set a real 32+ char key}"
export TEMPORAL_ADDRESS=localhost:7233 PLUGIN_MAGIC_COOKIE="${PLUGIN_MAGIC_COOKIE:?set a real cookie}"
export TEMPORAL_INIT_SEARCH_ATTRIBUTES=true STACK_PUBLIC_URL="${STACK_PUBLIC_URL:-http://localhost:8080}"

./payments-ce migrate up
setsid nohup ./payments-ce server > payments-server.log 2>&1 < /dev/null &
setsid nohup ./payments-ce worker --listen :8082 --profiler-listen :9191 > payments-worker.log 2>&1 < /dev/null &
for _ in $(seq 60); do curl -fsS localhost:8080/_healthcheck >/dev/null 2>&1 && break; sleep 1; done
curl -fsS localhost:8080/_healthcheck >/dev/null && echo "payments server ok"
for _ in $(seq 60); do curl -fsS localhost:8082/_healthcheck >/dev/null 2>&1 && break; sleep 1; done
curl -fsS localhost:8082/_healthcheck >/dev/null && echo "payments worker ok"
curl -fsS localhost:8080/v3/connectors/configs | python3 -c 'import sys,json;print(sorted(json.load(sys.stdin)["data"]))'
