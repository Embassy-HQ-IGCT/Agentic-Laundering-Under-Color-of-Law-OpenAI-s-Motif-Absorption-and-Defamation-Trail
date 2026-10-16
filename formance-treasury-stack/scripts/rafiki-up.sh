#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Rafiki (Interledger) native local bring-up: auth + backend against Postgres/Redis containers.
# Usage: rafiki-up.sh <formance-dir>   (needs git, docker, node 24 on PATH or $NODE24_BIN)
set -euo pipefail
ROOT="${1:?formance dir}"; cd "$ROOT"; ROOT="$PWD"
[ -n "${NODE24_BIN:-}" ] && export PATH="$NODE24_BIN:$PATH"
node -v | grep -q '^v24' || { echo "Node 24 required" >&2; exit 1; }
RUN="$ROOT/rafiki-run"; mkdir -p "$RUN"

[ -d rafiki ] || git clone --depth 1 https://github.com/interledger/rafiki.git
(cd rafiki && git submodule update --init --depth 1 open-payments-specifications \
  && pnpm install --frozen-lockfile >/dev/null && pnpm --filter backend --filter auth build >/dev/null)

docker rm -f rafiki-pg rafiki-redis >/dev/null 2>&1 || true
docker run -d --name rafiki-pg -p 5433:5432 -e POSTGRES_PASSWORD=password -e POSTGRES_USER=postgres \
  -v "$ROOT/rafiki/localenv/cloud-nine-wallet/dbinit.sql:/docker-entrypoint-initdb.d/init.sql" postgres:15 >/dev/null
docker run -d --name rafiki-redis -p 6379:6379 redis:7 >/dev/null
for _ in $(seq 60); do docker exec rafiki-pg psql -U postgres -d cloud_nine_wallet_backend -c 'select 1' >/dev/null 2>&1 && break; sleep 1; done

# mock ASE endpoints Rafiki calls: FX rates + webhooks
WEBHOOK_LOG="$RUN/webhooks.jsonl" setsid nohup python3 "$ROOT/mock_ase.py" > "$RUN/mock_ase.log" 2>&1 < /dev/null &

COMMON_SECRETS=(ADMIN_API_SECRET=iyIgCprjb9uL8wFckR+pLEkJWMB7FJhgkvqhTQR/964= OPERATOR_TENANT_ID=438fa74a-fa7d-4317-9ced-dde32ece1787)
(cd rafiki/packages/auth; env NODE_ENV=development "${COMMON_SECRETS[@]}" \
  AUTH_DATABASE_URL=postgresql://cloud_nine_wallet_auth:cloud_nine_wallet_auth@localhost:5433/cloud_nine_wallet_auth \
  AUTH_SERVER_URL=http://localhost:3006 REDIS_URL=redis://localhost:6379/1 \
  IDENTITY_SERVER_URL=http://localhost:3030/mock-idp/ IDENTITY_SERVER_SECRET=2pEcn2kkCclbOHQiGNEwhJ0rucATZhrA807HTm2rNXE= \
  COOKIE_KEY=42397d1f371dd4b8b7d0308a689a57c882effd4ea909d792302542af47e2cd37 SERVICE_API_PORT=3011 \
  setsid nohup node dist/index.js > "$RUN/auth.log" 2>&1 < /dev/null &)
for _ in $(seq 60); do curl -s -o /dev/null localhost:3006/ && break; sleep 1; done

(cd rafiki/packages/backend; env NODE_ENV=development "${COMMON_SECRETS[@]}" LOG_LEVEL=info INSTANCE_NAME=CLOUD-NINE \
  ADMIN_PORT=3001 CONNECTOR_PORT=3002 OPEN_PAYMENTS_PORT=3000 \
  DATABASE_URL=postgresql://cloud_nine_wallet_backend:cloud_nine_wallet_backend@localhost:5433/cloud_nine_wallet_backend \
  USE_TIGERBEETLE=false AUTH_SERVER_GRANT_URL=http://localhost:3006 AUTH_SERVER_INTROSPECTION_URL=http://localhost:3007 \
  AUTH_SERVICE_API_URL=http://localhost:3011 ILP_ADDRESS=test.cloud-nine-wallet \
  STREAM_SECRET=BjPXtnd00G2mRQwP/8ZpwyZASOch5sUXT5o0iR5b5wU= OPEN_PAYMENTS_URL=https://cloud-nine.local \
  WEBHOOK_URL=http://127.0.0.1:3100/webhooks EXCHANGE_RATES_URL=http://127.0.0.1:3100/rates \
  REDIS_URL=redis://localhost:6379/0 WALLET_ADDRESS_URL=https://cloud-nine.local/.well-known/pay \
  ILP_CONNECTOR_URL=http://localhost:3002 ENABLE_TELEMETRY=false KEY_ID=7097F83B-CB84-469E-96C6-2141C72E22C0 \
  DB_ENCRYPTION_SECRET='zO9KogehJECHReHgQr+ZWGkmgOD4AYa4ksUxALSwgM8=' \
  setsid nohup node dist/index.js > "$RUN/backend.log" 2>&1 < /dev/null &)
for _ in $(seq 90); do curl -fs localhost:3001/healthz >/dev/null 2>&1 && break; sleep 1; done
curl -fs localhost:3001/healthz >/dev/null && echo "rafiki backend ok"
curl -s -o /dev/null -w "rafiki auth http %{http_code}\n" localhost:3006/
