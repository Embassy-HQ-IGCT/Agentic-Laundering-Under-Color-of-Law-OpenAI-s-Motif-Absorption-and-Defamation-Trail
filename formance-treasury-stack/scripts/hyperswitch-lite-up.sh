#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Hyperswitch (Juspay) "Standalone / lite" profile, built from source: app server + Postgres + Redis only.
# Mirrors scripts/setup.sh's standalone profile (pg, redis, migrations, superposition, hyperswitch-server) but:
#   - builds the router binary locally (docker.juspay.io / ghcr.io images may be unreachable),
#   - applies SQL migrations with psql instead of the diesel CLI,
#   - skips the Superposition service: the router falls back to config/superposition_seed.toml.
# Usage: hyperswitch-lite-up.sh <hyperswitch-checkout> [router-binary]
set -euo pipefail
HS="$(cd "${1:?hyperswitch checkout}" && pwd)"; BIN="${2:-$HS/target/release-fast/router}"
RUN="${RUN_DIR:-$HS/../hyperswitch-run}"; mkdir -p "$RUN"
PG_PORT="${HS_PG_PORT:-5434}" REDIS_PORT="${HS_REDIS_PORT:-6380}" API_PORT="${HS_API_PORT:-8090}"
[ -x "$BIN" ] || { echo "router binary not found: $BIN (cargo build --profile release-fast --bin router)" >&2; exit 1; }

docker rm -f hs-pg hs-redis >/dev/null 2>&1 || true
docker run -d --name hs-pg -p "$PG_PORT:5432" -e POSTGRES_USER=db_user -e POSTGRES_PASSWORD=db_pass -e POSTGRES_DB=hyperswitch_db postgres:15 >/dev/null
docker run -d --name hs-redis -p "$REDIS_PORT:6379" redis:7 >/dev/null
for _ in $(seq 60); do PGPASSWORD=db_pass psql -h 127.0.0.1 -p "$PG_PORT" -U db_user -d hyperswitch_db -c 'select 1' >/dev/null 2>&1 && break; sleep 1; done

echo "applying $(ls "$HS"/migrations/*/up.sql | wc -l) migrations"
for f in $(ls -d "$HS"/migrations/*/ | sort); do
  PGPASSWORD=db_pass psql -q -v ON_ERROR_STOP=1 -h 127.0.0.1 -p "$PG_PORT" -U db_user -d hyperswitch_db -f "${f}up.sql" >/dev/null 2>>"$RUN/migrate.err" \
    || { echo "migration failed: $f (see $RUN/migrate.err)" >&2; exit 1; }
done

# local config: docker hostnames -> localhost, custom ports
sed -e 's|^host = "pg"|host = "127.0.0.1"|' -e "s|^port = 5432|port = $PG_PORT|" \
    -e 's|^host = "redis-standalone"|host = "127.0.0.1"|' \
    -e "0,/^port = 8080/s//port = $API_PORT/" "$HS/config/docker_compose.toml" > "$RUN/lite.toml"
# the [redis] block is the one using the redis port; patch only that block
python3 - "$RUN/lite.toml" "$REDIS_PORT" <<'EOF'
import re, sys
p, port = sys.argv[1], sys.argv[2]; s = open(p).read()
s = re.sub(r'(\[redis\]\nhost = "127.0.0.1"\nport = )\d+', r'\g<1>' + port, s, count=1)
open(p, "w").write(s)
EOF
cd "$HS"   # router resolves ./config/superposition_seed.toml relative to cwd
setsid nohup "$BIN" -f "$RUN/lite.toml" > "$RUN/router.log" 2>&1 < /dev/null &
for _ in $(seq 90); do curl -fs "localhost:$API_PORT/health" >/dev/null 2>&1 && break; sleep 1; done
curl -fs "localhost:$API_PORT/health" && echo " hyperswitch router healthy on :$API_PORT"
