#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Renders the platform manifests, extracts the PrometheusRule and runs promtool's rule lint and unit tests (gke/alerts-test.yaml).
# Needs: promtool on PATH (or PROMTOOL=...). Usage: gke/test-alerts.sh
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT
PROMTOOL="${PROMTOOL:-promtool}"
"$HERE/gke-deploy.sh" --render-only --non-interactive --config "$HERE/ci-answers.env" --out "$W/render" >/dev/null
NS=$(python3 - "$W/render/platform.yaml" "$W/alerts.rules.yaml" <<'PY'
import sys, yaml
r = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d and d.get("kind") == "PrometheusRule"][0]
yaml.safe_dump(r["spec"], open(sys.argv[2], "w"))
print(r["metadata"]["namespace"])
PY
)
sed "s/namespace=\"treasury\"/namespace=\"$NS\"/" "$HERE/alerts-test.yaml" > "$W/alerts-test.yaml"
cd "$W" && "$PROMTOOL" check rules alerts.rules.yaml && "$PROMTOOL" test rules alerts-test.yaml
