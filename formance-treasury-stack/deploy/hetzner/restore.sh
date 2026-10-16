#!/usr/bin/env bash
# Restore a backup directory produced by backup.sh into a stopped stack.  Usage: restore.sh /var/backups/treasury/<stamp>
set -euo pipefail
cd "$(dirname "$0")"
B="${1:?backup dir}"; (cd "$B" && sha256sum -c SHA256SUMS)
export SOPS_AGE_KEY_FILE="${SOPS_AGE_KEY_FILE:-$PWD/keys.txt}"
sops exec-env secrets.enc.env '
  docker compose down
  docker volume rm treasury_ledgerdata treasury_pgdata 2>/dev/null || true
  docker volume create treasury_ledgerdata >/dev/null
  docker run --rm -v treasury_ledgerdata:/data -v "'"$B"'":/in alpine tar -C /data -xzf /in/ledger.tgz
  docker compose up -d postgres
  until docker compose exec -T postgres pg_isready -U treasury -d treasury >/dev/null 2>&1; do sleep 1; done
  docker compose exec -T postgres pg_restore -U treasury -d treasury --clean --if-exists < "'"$B"'/postgres.dump"
  docker compose up -d
'
