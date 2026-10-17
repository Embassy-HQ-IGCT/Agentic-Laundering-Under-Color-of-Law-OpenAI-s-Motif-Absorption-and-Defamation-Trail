#!/usr/bin/env bash
# One-time: create the age key, generate every secret, and store them ENCRYPTED (sops+age) in secrets.enc.env.
# Needs: sops, age.  The age PRIVATE key (keys.txt) is the only thing that can decrypt - keep copies OFF this server.
set -euo pipefail
cd "$(dirname "$0")"
command -v sops >/dev/null && command -v age-keygen >/dev/null || { echo "install sops and age first"; exit 1; }
[ -e secrets.enc.env ] && { echo "secrets.enc.env exists; refusing to overwrite"; exit 1; }
[ -e keys.txt ] || { age-keygen -o keys.txt 2>/dev/null; chmod 600 keys.txt; echo "created keys.txt - BACK IT UP in two places that are not this server"; }
PUB=$(grep -o 'age1[a-z0-9]*' keys.txt | head -1)
: "${DOMAIN:?export DOMAIN=treasury.example.com}"
KEYS=$(python3 -c "import json,secrets;print(json.dumps({'ops':{'secret':secrets.token_urlsafe(48),'scopes':['*'],'rps':50}}))")
BANKS=${BANKS_JSON:-'[{"name":"bofa","type":"manual","rails":["FEDWIRE","SWIFT"],"ccys":["USD"]}]'}
umask 077
cat > .secrets.plain <<ENV
DOMAIN=$DOMAIN
POSTGRES_PASSWORD=$(openssl rand -hex 24)
TREASURY_API_KEYS='$KEYS'
RAFIKI_WEBHOOK_SECRET=$(openssl rand -base64 32)
BANKS_JSON='$BANKS'
ENV
sops --encrypt --age "$PUB" --input-type dotenv --output-type dotenv .secrets.plain > secrets.enc.env
shred -u .secrets.plain 2>/dev/null || rm -f .secrets.plain
echo "wrote secrets.enc.env (safe to commit). Decrypt with: SOPS_AGE_KEY_FILE=keys.txt sops -d secrets.enc.env"
