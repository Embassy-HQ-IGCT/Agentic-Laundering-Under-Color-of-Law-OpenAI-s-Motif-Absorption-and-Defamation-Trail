#!/usr/bin/env python3
"""Offline audit of the rendered platform manifests (no cluster needed). Fails if a production-hardening property is missing.
Usage: validate-manifests.py platform.yaml"""
import sys
import yaml

docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
by = {(d["kind"], d["metadata"]["name"]): d for d in docs}
errs = []


def need(cond, msg):
    if not cond:
        errs.append(msg)


eng = by.get(("Deployment", "treasury-engine"))
need(eng is not None, "treasury-engine Deployment missing")
if eng:
    spec, pod = eng["spec"], eng["spec"]["template"]["spec"]
    c = pod["containers"][0]
    need(spec.get("replicas", 1) >= 2, "engine must run >= 2 replicas")
    need(spec["strategy"]["rollingUpdate"]["maxUnavailable"] == 0, "rolling update must not reduce capacity (maxUnavailable: 0)")
    need(pod["securityContext"].get("runAsNonRoot") is True, "pod must runAsNonRoot")
    need(pod["securityContext"].get("seccompProfile", {}).get("type") == "RuntimeDefault", "pod seccomp RuntimeDefault required")
    sc = c["securityContext"]
    need(sc.get("readOnlyRootFilesystem") is True, "engine rootfs must be read-only")
    need(sc.get("allowPrivilegeEscalation") is False, "privilege escalation must be disabled")
    need(sc.get("capabilities", {}).get("drop") == ["ALL"], "all capabilities must be dropped")
    for p in ("startupProbe", "livenessProbe", "readinessProbe"):
        need(p in c, f"engine missing {p}")
    need("limits" in c["resources"] and "requests" in c["resources"], "engine needs resource requests and limits")
    env = {e["name"]: e for e in c["env"]}
    need(env.get("TREASURY_ENV", {}).get("value") == "prod", "TREASURY_ENV must be prod")
    for secret_env in ("DATABASE_URL", "TREASURY_API_KEYS", "RAFIKI_WEBHOOK_SECRET"):
        need("valueFrom" in env.get(secret_env, {}), f"{secret_env} must come from a Secret, not a literal")
    for forbidden in ("TREASURY_ADMIN", "TREASURY_TEST_CLOCK"):
        need(forbidden not in env, f"{forbidden} must not be set")
    need(pod.get("terminationGracePeriodSeconds", 30) >= 20, "terminationGracePeriodSeconds too short for graceful drain")
need(("PodDisruptionBudget", "treasury-engine") in by, "PodDisruptionBudget for the engine missing")
need(("NetworkPolicy", "treasury-engine") in by, "NetworkPolicy for the engine missing")
need(("Job", "ledger-bootstrap") in by, "ledger-bootstrap Job (creates the required reference index) missing")
need(all("${" not in yaml.safe_dump(d) for d in docs), "unrendered ${VARIABLE} left in manifests")
if errs:
    print("MANIFEST AUDIT FAILED:\n  - " + "\n  - ".join(errs))
    sys.exit(1)
print(f"manifest audit passed ({len(docs)} objects; engine: {eng['spec']['replicas']} replicas, non-root, read-only rootfs, caps dropped, probes, PDB, NetworkPolicy)")
