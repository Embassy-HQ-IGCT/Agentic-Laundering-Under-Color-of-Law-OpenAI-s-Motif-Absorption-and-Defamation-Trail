#!/usr/bin/env bash
# Start/refresh the stack with secrets decrypted into the process environment only (never written to disk).
set -euo pipefail
cd "$(dirname "$0")"
export SOPS_AGE_KEY_FILE="${SOPS_AGE_KEY_FILE:-$PWD/keys.txt}"
exec sops exec-env secrets.enc.env "docker compose up -d --build ${*:-}"
