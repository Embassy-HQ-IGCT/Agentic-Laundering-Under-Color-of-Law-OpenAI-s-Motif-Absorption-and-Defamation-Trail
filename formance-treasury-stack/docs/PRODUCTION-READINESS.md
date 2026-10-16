# Production readiness

**Verdict: the engine is production-grade *engineering*; the system is not yet production-*approved*.** The software controls below are built and
tested. Go-live additionally needs things only you can supply (real bank adapters, security review, compliance, a real cluster run). The last
section is the blocking checklist.

## 1. Engineered and verified (181 automated checks, run on SQLite **and** PostgreSQL, plus a hardened run)

| Area | Control | Evidence |
|---|---|---|
| **No lost / double money on crash** | Payouts are a write-ahead state machine (`new -> reserved -> routing -> settled/accepted/review/failed`). Every Ledger step has a deterministic reference; the bank call is idempotent on the payout id; the intent is persisted *before* the bank is called; a lease stops two workers driving one payout. | Process killed at 5 points (after insert / reserve / route / submit / settle-tx): recovery drives each to `settled`, wallet debited **exactly once**, bank got **at most one** instruction, ledger sums to 0. |
| **Unknown bank outcome never double-pays** | Timeout *after* send -> `review` (funds stay committed), never failed over. Resolution asks the bank by payout id (`lookup`): found -> adopt; definitively absent -> release funds. A resumed attempt never fails over on "connection refused". | Tested for both "reply lost after bank accepted" and "request lost before bank saw it". |
| **Idempotency bound to the request** | Same id + different content -> **409**, never a silent "duplicate". Ledger transactions carry a content fingerprint; payouts / authorizations / captures are fingerprinted too. | Transfers, payouts, authorizations, captures. |
| **Card authorization correctness** | Hold is one atomic Ledger transaction; concurrent auths cannot overspend. A crash between hold and decision-record is recovered **from the Ledger** (split between wallet and credit line is read back, not guessed); a hold is adopted even if the card was blocked in between (no leaked holds). Capture/reverse accounting is event-based and replay-safe. | 60 parallel auths on a 10.00 wallet -> exactly 10 approved; crash/replay tests. p50 ≈ 11 ms, p95 ≈ 13-18 ms (HTTP, sandbox). |
| **Authentication / authorization** | Every route (except `/livez`, `/readyz`) needs an HMAC-SHA256 signed request over method + path + body hash + timestamp (±300 s replay window), constant-time compare, one error for all failure modes. Per-client keys with **scopes** (least privilege) and per-key rate limits. | Missing/bad/unknown key, stale timestamp, tampered body, tampered path -> 401; missing scope -> 403; burst -> 429. |
| **Webhook authentication** | Rafiki webhooks verified with Rafiki's own scheme (`Rafiki-Signature: t, v1`); our JSON canonicalization cross-checked against Rafiki's `json-canonicalize` on real recorded events. | Unsigned / wrong secret / stale -> 401; redelivery idempotent. |
| **Input validation** | Strict ids, ISO currency, positive integer minor units with a hard ceiling, object-only bodies, 64 KB body cap (413), destination types. Unknown ids -> 404; real bugs surface as 500 (no `KeyError` -> 400 masking). | 13 negative-input tests. |
| **State store** | PostgreSQL for production (required; SQLite refused), `ANSI` SQL shared with SQLite for tests, reconnect-once on dropped connections. | Identical suites pass on both. |
| **Safe startup** | Production refuses to start with: no API keys, weak key secrets, SQLite state, test clock / admin endpoints enabled, mock banks, or a Ledger lacking the required `reference` index. | 6 refusal tests. |
| **Operations** | `/livez`, `/readyz` (Ledger + DB + not draining), Prometheus metrics on a separate internal port, JSON access logs with request id + actor, append-only audit table, actor/request id/fingerprint stamped into every Ledger transaction, graceful SIGTERM drain, background recovery loop (resume stuck payouts, resolve unknowns, expire holds). | Shutdown, metrics, audit and Ledger-metadata tests. |
| **Privilege** | Runs as uid 10001 from a **read-only** source tree with no writable paths (state in Postgres). | Full suite passes in that mode. |
| **Manifests** | Offline audit (`gke/validate-manifests.py`) fails if the engine loses: >=2 replicas, zero-downtime rollout, non-root, read-only rootfs, dropped capabilities, seccomp, probes, resource limits, secret-sourced credentials, PDB, NetworkPolicy, the Ledger bootstrap job. Negative-control tested. | Passes on the rendered output; rejects a weakened one. |
| **Infra as code** | Infrastructure as code (OpenTofu preferred, Terraform works; both validate): private VPC + NAT, private GKE Autopilot (authorized networks), Cloud SQL Postgres 15 (private IP, HA, PITR backups, deletion protection), Memorystore, Artifact Registry, Secret Manager, Workload Identity. | `tofu validate` and `terraform validate` pass. |

## 2. Known limitations of what is built

- **Rate limiting is per replica** (in-memory token bucket): the effective global limit is `replicas x rps`.
- **API keys load at start**; rotating means a rolling restart (add a new Secret Manager version, re-run the deploy script).
- **Audit table lives in the same database** (append-only by convention, not WORM). Ship logs/audit to immutable storage for compliance.
- **The Ledger is a single Raft node.** Money history is durable on its volume, but a node loss is downtime until restore; HA (3 voters) is untested.
- Card **controls are basic** (per-txn, daily, MCC, country). No velocity/fraud scoring, no sanctions or AML screening.
- The engine never touches card numbers (`card_id` tokens only). Keep it that way: PANs would put it in PCI scope.
- FX assumes 2-decimal currencies and static rates.
- Defaults for rail hours/limits and each bank's rail participation are **assumptions** (`rails.py`, mock bank config).

## 3. Not verified here (must be done before go-live)

1. **Container image** build/run: Docker Hub rate-limited the base image in the sandbox. The same code ran as uid 10001 from a read-only tree, but the image itself is untested.
2. **Any real GCP run**: `gke-deploy.sh --plan/--apply`, the Kubernetes manifests, Rafiki Helm values, the NetworkPolicy labels, Workload Identity, Cloud SQL SSL. Validated offline only.
3. **Real bank adapters** (BofA, Navy Federal) and a real issuer-processor for cards. Only mocks exist. Adapters must satisfy the `BankAdapter` contract in `banks.py` (idempotent submit, `lookup`, honest `sent` semantics).
4. **Live Rafiki -> engine webhooks** with Rafiki's signing secret configured (scheme verified against Rafiki's source, not a live instance).
5. **Load, soak, chaos and DR drills**; Ledger HA; backup/restore rehearsal (Cloud SQL PITR, Ledger snapshots).
6. **Independent security review / penetration test.**

## 4. Go-live checklist (blocking)

- [ ] **Licensing:** obtain written licence terms for Formance Ledger v3 (none were published in the tested revision) or switch to a licensed ledger; see `THIRD_PARTY.md`.
- [ ] Real bank adapters implemented and certified against each bank's sandbox; idempotency + `lookup` proven with the bank.
- [ ] Bank-confirmed rail participation, limits and cutoffs loaded into config (`TREASURY_RAILS_JSON`, `TREASURY_HOLIDAYS`).
- [ ] `gke-deploy.sh --plan` reviewed, then `--apply` in a staging project; full suite re-run against staging.
- [ ] Rafiki `SIGNATURE_SECRET` set to the `*-rafiki-webhook-secret` value; end-to-end Rafiki payment observed posting to the Ledger.
- [ ] Images pinned by digest; dependency pins hashed; SBOM/vulnerability scan in CI.
- [ ] Alerts wired (below); on-call runbook for `review` payouts and reconciliation breaks.
- [ ] Penetration test; legal/compliance sign-off (money transmission/banking licences, card programme rules, KYC/AML/sanctions).
- [ ] Ledger HA and restore drill; Cloud SQL failover drill.

## 5. Suggested alerts (Prometheus)

| Alert | Expression (sketch) |
|---|---|
| Payout outcome unknown | `treasury_payouts_needing_attention > 0` for 10 m |
| Payouts stuck | `treasury_payouts{status=~"new\|reserved\|routing"} > 0` for 10 m (recovery loop should clear within ~1 m) |
| Engine not ready | `kube_deployment_status_replicas_available{deployment="treasury-engine"} < 2` |
| Error rate | `sum(rate(treasury_http_requests_total{status=~"5.."}[5m])) / sum(rate(treasury_http_requests_total[5m])) > 0.01` |
| Authorization latency | `histogram_quantile(0.99, rate(treasury_http_request_seconds_bucket{route="/card/authorize"}[5m])) > 0.1` |
| Auth attack / misconfig | `rate(treasury_http_requests_total{status=~"401\|403"}[5m]) > 1` |
| Decline spike | `rate(treasury_card_decisions_total{approved="false"}[5m])` vs baseline |
