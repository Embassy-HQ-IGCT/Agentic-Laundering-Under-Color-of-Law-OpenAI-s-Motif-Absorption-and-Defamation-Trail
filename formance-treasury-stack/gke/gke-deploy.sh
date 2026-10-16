#!/usr/bin/env bash
# gke-deploy.sh - collect GKE cluster details, then provision and deploy the treasury stack:
#   Terraform  : VPC, private GKE Autopilot, Cloud SQL (Postgres), Memorystore (Redis), Artifact Registry, Secret Manager, Workload Identity
#   kubectl    : Temporal, Formance Payments (community), Formance Ledger v3, Rafiki->Ledger bridge
#   helm       : Rafiki auth / backend / frontend (Interledger charts)
#
# Modes (default is --plan: nothing is created or changed in Google Cloud):
#   --render-only   no cloud calls at all; collect answers, render + validate files into --out
#   --plan          preflight + terraform plan (read-only)
#   --apply         provision + deploy; requires typing the project ID to confirm
# Other flags: --config FILE (answers file)  --non-interactive  --out DIR
# Secrets are never written to disk by this script: they are generated into / read from Secret Manager.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
MODE=plan; CONFIG=""; INTERACTIVE=1; OUT="$HERE/out"
while [ $# -gt 0 ]; do case "$1" in
  --render-only) MODE=render ;; --plan) MODE=plan ;; --apply) MODE=apply ;;
  --config) CONFIG="$2"; shift ;; --non-interactive) INTERACTIVE=0 ;; --out) OUT="$2"; shift ;;
  -h|--help) sed -n '2,15p' "$0"; exit 0 ;; *) echo "unknown flag $1" >&2; exit 2 ;; esac; shift; done
mkdir -p "$OUT"
[ -n "$CONFIG" ] && { set -a; . "$CONFIG"; set +a; }

say(){ printf '\n== %s\n' "$*"; }
die(){ echo "ERROR: $*" >&2; exit 1; }
ask(){ # ask VAR "Prompt" default [secret-free validation regex]
  local var="$1" prompt="$2" def="${3:-}" re="${4:-.*}" cur="${!1:-}" ans
  [ -n "$cur" ] && def="$cur"
  while :; do
    if [ "$INTERACTIVE" = 1 ]; then read -r -p "$prompt${def:+ [$def]}: " ans || true; ans="${ans:-$def}"; else ans="$def"; fi
    [[ "$ans" =~ $re ]] && [ -n "$ans" ] && { printf -v "$var" '%s' "$ans"; export "$var"; return; }
    [ "$INTERACTIVE" = 1 ] && echo "  invalid or empty, try again" || die "$var missing or invalid (--non-interactive)"
  done
}
yn(){ local a; ask a "$1 (y/n)" "$2" '^[yn]$'; [ "$a" = y ]; }

say "Cluster details"
ask PROJECT_ID     "GCP project ID"                          ""            '^[a-z][a-z0-9-]{4,28}[a-z0-9]$'
ask REGION         "Region"                                  "us-central1" '^[a-z]+-[a-z]+[0-9]$'
ask NAME           "Name prefix (cluster + resources)"       "treasury"    '^[a-z][a-z0-9-]{1,20}$'
ask NAMESPACE      "Kubernetes namespace"                    "treasury"    '^[a-z][a-z0-9-]{1,30}$'
ask ADMIN_CIDR     "Admin/CI egress CIDR allowed to reach the GKE control plane (e.g. 203.0.113.4/32)" "" '^[0-9.]+/[0-9]+$'
ask SQL_TIER       "Cloud SQL machine tier"                  "db-custom-2-7680"
ask SQL_HA         "Cloud SQL high availability (true/false)" "true"      '^(true|false)$'
ask REDIS_TIER     "Memorystore tier (BASIC|STANDARD_HA)"    "STANDARD_HA" '^(BASIC|STANDARD_HA)$'
ask DELETION_PROT  "Deletion protection on cluster/DB (true/false)" "true" '^(true|false)$'

say "Public endpoints (DNS names you control; TLS/ingress is configured separately)"
ask RAFIKI_PUBLIC_URL     "Rafiki Open Payments base URL (https)"   "" '^https://[a-z0-9.-]+$'
ask RAFIKI_AUTH_PUBLIC_URL "Rafiki auth server public URL (https)"   "" '^https://[a-z0-9.-]+$'
ask PAYMENTS_PUBLIC_URL   "Formance Payments public URL (webhooks/redirects from PSPs)" "" '^https://[a-z0-9.-]+$'
ask IDP_URL               "Identity provider URL for Rafiki consent screens" "" '^https://'
ask ILP_ADDRESS           "Interledger address of this node (e.g. g.yourbank)" "" '^[a-z0-9._-]+$'
ask EXCHANGE_RATES_URL    "FX rates endpoint Rafiki should call (returns {base, rates}); default = the treasury engine" "http://treasury-engine.${NAMESPACE}:8200/rates" '^https?://'

say "Components"
ask LEDGER_IMAGE   "Formance Ledger image (pin a tag)"   "ghcr.io/formancehq/ledger:latest"
ask PAYMENTS_IMAGE "Formance Payments image (pin a tag)" "ghcr.io/formancehq/payments:latest"
ask LEDGER_DISK_GI "Ledger disk size (GiB)" "100" '^[0-9]+$'
ask BANK_SECRETS   "Bank/PSP credential secret names to create empty in Secret Manager (comma-separated, may be blank)" "stripe,bank-primary,bank-secondary" '^[a-z0-9,-]*$'

ask ALLOW_MOCK_BANKS "Run the treasury engine with SIMULATED banks? 1 = sandbox/staging ONLY (no real money moves); 0 = require real bank adapters" "0" '^[01]$'
export LEDGER_CLUSTER_ID="${NAME}-ledger" KSA="${NAME}-app" APP_GSA="${NAME}-app@${PROJECT_ID}.iam.gserviceaccount.com"
export REGISTRY="${REGION}-docker.pkg.dev/${PROJECT_ID}/${NAME}-images"
export BRIDGE_IMAGE="${REGISTRY}/rafiki-ledger-bridge:1" ENGINE_IMAGE="${REGISTRY}/treasury-engine:1"
export INGRESS_NAMESPACE="${INGRESS_NAMESPACE:-ingress-nginx}" MONITORING_NAMESPACE="${MONITORING_NAMESPACE:-monitoring}" VPC_CIDR="${VPC_CIDR:-10.0.0.0/8}"

say "Rendering files into $OUT"
BANKS_JSON=$(python3 -c "import json,os;print(json.dumps([s for s in os.environ['BANK_SECRETS'].split(',') if s]))")
cat > "$OUT/terraform.tfvars.json" <<EOF
{ "project_id": "$PROJECT_ID", "region": "$REGION", "name": "$NAME", "namespace": "$NAMESPACE",
  "k8s_service_account": "$KSA", "sql_tier": "$SQL_TIER", "sql_high_availability": $SQL_HA,
  "redis_tier": "$REDIS_TIER", "deletion_protection": $DELETION_PROT,
  "master_authorized_cidrs": [{ "cidr": "$ADMIN_CIDR", "name": "admin" }], "bank_secret_names": $BANKS_JSON }
EOF
# Answers file (no secrets) so the run is repeatable:
{ for v in PROJECT_ID REGION NAME NAMESPACE ADMIN_CIDR SQL_TIER SQL_HA REDIS_TIER DELETION_PROT RAFIKI_PUBLIC_URL \
  RAFIKI_AUTH_PUBLIC_URL PAYMENTS_PUBLIC_URL IDP_URL ILP_ADDRESS EXCHANGE_RATES_URL LEDGER_IMAGE PAYMENTS_IMAGE LEDGER_DISK_GI BANK_SECRETS ALLOW_MOCK_BANKS; do
  printf '%s=%q\n' "$v" "${!v}"; done; } > "$OUT/answers.env"

# Placeholders for values that only exist after Terraform runs; replaced with real outputs in --apply.
: "${SQL_IP:=<from-terraform>}" "${REDIS_HOST:=<from-terraform>}" "${REDIS_PORT:=6379}"
export SQL_IP REDIS_HOST REDIS_PORT
envsubst < "$HERE/k8s/platform.yaml.tmpl" > "$OUT/platform.yaml"
envsubst < "$HERE/k8s/rafiki-values.yaml.tmpl" > "$OUT/rafiki-values.yaml"
python3 "$HERE/validate-manifests.py" "$OUT/platform.yaml" || die "rendered manifests failed the hardening checks"
python3 - "$OUT" <<'EOF'
import sys, yaml
out = sys.argv[1]; v = yaml.safe_load(open(f"{out}/rafiki-values.yaml"))
for chart in ("rafiki-backend", "rafiki-auth", "rafiki-frontend"):
    yaml.safe_dump(v[chart], open(f"{out}/{chart}.values.yaml", "w"))
list(yaml.safe_load_all(open(f"{out}/platform.yaml")))
print("rendered: terraform.tfvars.json platform.yaml rafiki-{backend,auth,frontend}.values.yaml")
EOF

if command -v terraform >/dev/null; then
  ( cd "$HERE/terraform" && terraform init -backend=false >/dev/null 2>&1 && terraform validate >/dev/null && echo "terraform config valid" ) || echo "WARN: terraform validate did not pass (provider mirror/registry access?)"
fi
[ "$MODE" = render ] && { echo "render-only: done. Review $OUT"; exit 0; }

say "Preflight"
for t in gcloud terraform kubectl helm envsubst python3; do command -v "$t" >/dev/null || die "missing tool: $t"; done
ACCT=$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | head -1)
[ -n "$ACCT" ] || die "no active gcloud account: run 'gcloud auth login' and 'gcloud auth application-default login'"
echo "gcloud account: $ACCT"
gcloud projects describe "$PROJECT_ID" --format='value(projectId)' >/dev/null || die "cannot access project $PROJECT_ID"
BILL=$(gcloud billing projects describe "$PROJECT_ID" --format='value(billingEnabled)' 2>/dev/null || true)
[ "$BILL" = "True" ] || die "billing is not enabled on $PROJECT_ID (or no permission to check it)"
gcloud compute regions describe "$REGION" --project "$PROJECT_ID" --format='value(name)' >/dev/null || die "unknown region $REGION"
echo "preflight ok"

say "Terraform plan"
( cd "$HERE/terraform" && terraform init -input=false >/dev/null && terraform plan -input=false -var-file="$OUT/terraform.tfvars.json" -out="$OUT/tfplan" )
[ "$MODE" = plan ] && { echo "plan only: nothing was created. Re-run with --apply to provision and deploy."; exit 0; }

say "Confirm"
echo "About to PROVISION billable resources in project '$PROJECT_ID' ($REGION) and deploy workloads."
read -r -p "Type the project ID to continue: " c; [ "$c" = "$PROJECT_ID" ] || die "confirmation did not match; aborting"

say "Terraform apply"
( cd "$HERE/terraform" && terraform apply -input=false "$OUT/tfplan" )
tf(){ ( cd "$HERE/terraform" && terraform output -raw "$1" ); }
SQL_IP=$(tf sql_private_ip); REDIS_HOST=$(tf redis_host); REDIS_PORT=$(tf redis_port); export SQL_IP REDIS_HOST REDIS_PORT
gcloud container clusters get-credentials "$NAME" --region "$REGION" --project "$PROJECT_ID"

say "Secrets (generated into Secret Manager if absent; applied to the cluster)"
ensure(){ gcloud secrets describe "$1" --project "$PROJECT_ID" >/dev/null 2>&1 || {
  gcloud secrets create "$1" --project "$PROJECT_ID" --replication-policy=automatic >/dev/null
  openssl rand -base64 32 | tr -d '\n' | gcloud secrets versions add "$1" --project "$PROJECT_ID" --data-file=- >/dev/null; }
  gcloud secrets versions access latest --secret "$1" --project "$PROJECT_ID"; }
dbpw(){ gcloud secrets versions access latest --secret "${NAME}-db-$1" --project "$PROJECT_ID"; }
kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
CEK=$(ensure "${NAME}-payments-config-key"); COOKIE=$(ensure "${NAME}-payments-plugin-cookie")
kubectl -n "$NAMESPACE" create secret generic db-passwords --from-literal=temporal="$(dbpw temporal)" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n "$NAMESPACE" create secret generic app-secrets \
  --from-literal=payments-postgres-uri="postgres://payments:$(dbpw payments)@${SQL_IP}:5432/payments?sslmode=disable" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
# Engine credentials. Each API client gets its OWN key with minimal scopes; the key material lives in Secret Manager
# (${NAME}-engine-key-<client>) so clients fetch it from there. Rotate by adding a secret version and re-running this script.
K_CARD=$(ensure "${NAME}-engine-key-card-processor"); K_API=$(ensure "${NAME}-engine-key-payments-api"); K_OPS=$(ensure "${NAME}-engine-key-ops")
API_KEYS=$(python3 - "$K_CARD" "$K_API" "$K_OPS" <<'KEYEOF'
import json, sys
c, a, o = sys.argv[1:]
keys = {
  "card-processor": {"secret": c, "scopes": ["card"], "rps": 500},
  "payments-api":   {"secret": a, "scopes": ["read", "transfer", "payout", "fx", "credit", "guarantee", "sweep"], "rps": 200},
  "ops":            {"secret": o, "scopes": ["read", "payout:admin", "card:admin", "credit:admin", "recon", "webhook:bank"], "rps": 50},
}
assert all(len(k["secret"]) >= 32 for k in keys.values()), "generated key too short"
print(json.dumps(keys))
KEYEOF
)
RAFIKI_WH=$(ensure "${NAME}-rafiki-webhook-secret")
kubectl -n "$NAMESPACE" create secret generic treasury-engine-env \
  --from-literal=DATABASE_URL="postgresql://treasury:$(dbpw treasury)@${SQL_IP}:5432/treasury?sslmode=require" \
  --from-literal=TREASURY_API_KEYS="$API_KEYS" --from-literal=RAFIKI_WEBHOOK_SECRET="$RAFIKI_WH" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n "$NAMESPACE" create secret generic payments-env \
  --from-literal=POSTGRES_URI="postgres://payments:$(dbpw payments)@${SQL_IP}:5432/payments?sslmode=disable" \
  --from-literal=CONFIG_ENCRYPTION_KEY="$CEK" --from-literal=PLUGIN_MAGIC_COOKIE="$COOKIE" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

say "Bridge image (Cloud Build)"
BCTX=$(mktemp -d); cp "$HERE/k8s/Dockerfile.bridge" "$HERE/k8s/rafiki_formance_bridge.py" "$BCTX/"
cat > "$BCTX/cloudbuild.yaml" <<EOF
steps: [{ name: gcr.io/cloud-builders/docker, args: [build, --build-arg, "LEDGER_IMAGE=${LEDGER_IMAGE}", -f, Dockerfile.bridge, -t, "${BRIDGE_IMAGE}", .] }]
images: ["${BRIDGE_IMAGE}"]
EOF
gcloud builds submit "$BCTX" --project "$PROJECT_ID" --config "$BCTX/cloudbuild.yaml"; rm -rf "$BCTX"

say "Treasury engine image (Cloud Build)"
ECTX=$(mktemp -d); cp "$HERE"/../treasury/{ledger,rails,banks,engine,server,store}.py "$HERE/../treasury/requirements.txt" "$HERE/../treasury/Dockerfile" "$ECTX/"
cat > "$ECTX/cloudbuild.yaml" <<CBEOF
steps: [{ name: gcr.io/cloud-builders/docker, args: [build, -t, "${ENGINE_IMAGE}", .] }]
images: ["${ENGINE_IMAGE}"]
CBEOF
gcloud builds submit "$ECTX" --project "$PROJECT_ID" --config "$ECTX/cloudbuild.yaml"; rm -rf "$ECTX"

say "Deploy platform workloads"
envsubst < "$HERE/k8s/platform.yaml.tmpl" > "$OUT/platform.yaml"
kubectl apply -f "$OUT/platform.yaml"
kubectl -n "$NAMESPACE" wait --for=condition=complete job/payments-migrate --timeout=600s
kubectl -n "$NAMESPACE" wait --for=condition=complete job/ledger-bootstrap --timeout=600s
for d in temporal payments-server payments-worker rafiki-ledger-bridge; do kubectl -n "$NAMESPACE" rollout status deploy/$d --timeout=600s; done
kubectl -n "$NAMESPACE" rollout status sts/ledger --timeout=600s
if [ "$ALLOW_MOCK_BANKS" = 1 ]; then kubectl -n "$NAMESPACE" rollout status sts/treasury-engine --timeout=300s
else echo "NOTE: treasury-engine will not become ready until a real bank adapter is configured (it refuses to run with simulated banks)."; fi

say "Deploy Rafiki (Interledger charts)"
envsubst < "$HERE/k8s/rafiki-values.yaml.tmpl" > "$OUT/rafiki-values.yaml"
python3 - "$OUT" <<'EOF'
import sys, yaml
out = sys.argv[1]; v = yaml.safe_load(open(f"{out}/rafiki-values.yaml"))
for c in ("rafiki-backend", "rafiki-auth", "rafiki-frontend"): yaml.safe_dump(v[c], open(f"{out}/{c}.values.yaml", "w"))
EOF
helm repo add interledger https://interledger.github.io/helm-charts >/dev/null; helm repo update >/dev/null
STREAM=$(ensure "${NAME}-rafiki-stream-secret"); QUOTE=$(ensure "${NAME}-rafiki-quote-secret"); COOKIEK=$(ensure "${NAME}-rafiki-cookie-key"); IDSEC=$(ensure "${NAME}-rafiki-idp-secret")
helm upgrade --install rafiki-auth interledger/rafiki-auth -n "$NAMESPACE" -f "$OUT/rafiki-auth.values.yaml" \
  --set postgresql.password="$(dbpw rafiki-auth)" --set cookieKey="$COOKIEK" --set identityServer.secret="$IDSEC"
helm upgrade --install rafiki-backend interledger/rafiki-backend -n "$NAMESPACE" -f "$OUT/rafiki-backend.values.yaml" \
  --set postgresql.password="$(dbpw rafiki-backend)" --set ilp.streamSecret="$STREAM" --set quoteSignatureSecret="$QUOTE"
helm upgrade --install rafiki-frontend interledger/rafiki-frontend -n "$NAMESPACE" -f "$OUT/rafiki-frontend.values.yaml"

say "Smoke checks"
kubectl -n "$NAMESPACE" get pods
kubectl -n "$NAMESPACE" exec deploy/payments-server -- wget -qO- http://localhost:8080/_healthcheck >/dev/null && echo "payments healthy"
kubectl -n "$NAMESPACE" run ledger-smoke --rm -i --restart=Never --image="$LEDGER_IMAGE" --command -- \
  /app/ledgerctl --insecure --server ledger-0.ledger:8888 ledgers list >/dev/null && echo "ledger reachable"
echo
echo "IMPORTANT: configure Rafiki's webhook signing secret (SIGNATURE_SECRET) to the value of Secret Manager secret ${NAME}-rafiki-webhook-secret"
echo "           (the Rafiki chart key for it is not verified); until then the engine rejects every Rafiki webhook by design."
echo "Done. Next steps (manual, by design): add bank/PSP credential versions to the *-bank-* secrets, point DNS + TLS at the services,"
echo "create the 'rafiki' ledger (ledgerctl ledgers create rafiki), then install your bank connectors via the Payments API."
