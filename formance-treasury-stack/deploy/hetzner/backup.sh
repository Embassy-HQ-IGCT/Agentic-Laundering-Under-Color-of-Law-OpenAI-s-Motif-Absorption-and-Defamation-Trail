#!/usr/bin/env bash
# Nightly backup (cron): Postgres dump + a consistent copy of the Ledger volume (brief stop - the v3 Ledger keeps its data on a local
# volume, not in Postgres). Ship $OUT off-box (e.g. restic/rclone to Hetzner Storage Box or S3) and TEST the restore (restore.sh).
set -euo pipefail
cd "$(dirname "$0")"
export SOPS_AGE_KEY_FILE="${SOPS_AGE_KEY_FILE:-$PWD/keys.txt}"
OUT="${OUT:-/var/backups/treasury}/$(date -u +%Y%m%dT%H%M%SZ)"; mkdir -p "$OUT"
sops exec-env secrets.enc.env '
  docker compose exec -T postgres pg_dump -U treasury -Fc treasury > "'"$OUT"'/postgres.dump"
  docker compose stop ledger
  docker run --rm -v treasury_ledgerdata:/data:ro -v "'"$OUT"'":/out alpine tar -C /data -czf /out/ledger.tgz .
  docker compose start ledger
'
sha256sum "$OUT"/* > "$OUT/SHA256SUMS"; echo "backup written to $OUT"
