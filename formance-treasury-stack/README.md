# Formance treasury stack

Ledger-first treasury, payments and card-authorization engine on **Formance Community Edition (MIT)**, with **Rafiki** (Interledger / Open Payments) in front and bank rails behind. Built to run on **GKE**.

> **Status: hardened and sandbox-verified; not yet production-approved.** The engine has crash-safe money flows, request-bound idempotency,
> signed-request auth with scopes, Postgres state, observability and graceful shutdown, verified by **181 automated checks** (SQLite and
> PostgreSQL, plus a run as an unprivileged user from a read-only tree). Bank connectivity is still **simulated**, and nothing has run against a real
> cluster, bank or card network. See **[docs/PRODUCTION-READINESS.md](docs/PRODUCTION-READINESS.md)** for exactly what is and is not covered, and the go-live checklist.

## What it does

One service (`treasury/`) on top of the Ledger. **Every balance lives in the Ledger**; SQLite only holds workflow state (payout/auth records, card controls, quotes, facility terms).

| Capability | Behaviour | Status |
|---|---|---|
| **Card pre-authorization** | Real-time approve/decline from wallet availability, optionally backed by a credit line (wallet first, then credit). Hold → capture (partial, remainder released) → reverse → expire. ISO-8583-style response codes (00/51/61/62). Idempotent per `auth_id`. | tested |
| **No overspend under load** | 60 simultaneous authorizations against a 10.00 wallet → exactly 10 approved (Ledger refuses the rest atomically). | tested |
| **Transfers** | Instant wallet-to-wallet, idempotent by `ref`. | tested |
| **Payouts + rail routing** | FedNow / RTP (24x7x365), Fedwire (weekday windows), ACH (cutoffs, next-business-day), SWIFT (international, value-dated). Chooses bank + rail by rail hours, limits, bank support, liquidity; fails over across banks and rails. | tested (mock banks) |
| **After-hours execution** | Saturday 03:00 ET payout settles instantly on FedNow; oversize amount queues on ACH for Monday; Fedwire correctly closed Fri 19:00 → Sun 21:00 ET. | tested |
| **Safe failover** | "Connection refused" (not sent) fails over; "timeout after send" (outcome unknown) is parked for review and **never retried elsewhere** (no double-pay). | tested |
| **FX** | Quote-locked conversion with spread; atomic two-leg posting against FX inventory pools (pool balances = FX exposure); expiry; single use. | tested |
| **Credit, loans, guarantees** | Facilities with limits (Numscript overdraft), draw / repay / daily interest accrual; guarantees with partial cash collateral, claims (collateral → applicant → receivable), release. | tested |
| **Sweeps & liquidity** | Full sweep (Numscript `[CCY *]`) or sweep-above-target; per-bank liquidity view with rebalancing suggestions. | tested |
| **Reconciliation** | Applies statement credits (suspense if unattributable), matches debits to settled payouts, flags stray debits / mismatches; idempotent re-runs. | tested |
| **Rafiki intake** | Rafiki webhooks (replayed from real recorded events) post into the same wallets, idempotent on event id. | tested (recorded events) |
| **Ledger integrity** | Sum of all balances incl. `world` is exactly 0 for USD and EUR after the whole run. | tested |

Measured here: card authorization **p50 ≈ 11 ms, p95 ≈ 13-18 ms** end to end over HTTP including signature verification and audit (single node, sandbox).

**Fast path and every blocker we hit (with fixes): [docs/RUNBOOK.md](docs/RUNBOOK.md).**

## Run the tests

```bash
git clone https://github.com/formancehq/ledger && (cd ledger && go build -o ledger-bin . && go build -o ledgerctl ./cmd/ledgerctl)
LEDGER_BIN=$PWD/ledger/ledger-bin LEDGERCTL=$PWD/ledger/ledgerctl ./treasury/run-local-tests.sh   # SQLite;  expect: ALL SUITES PASSED
# same suites on PostgreSQL:        PG_URL=postgresql://user:pw@host:5432/db  (pip install -r treasury/requirements.txt)
# as the container will run it:     HARDENED_RUN=1  (uid 10001, read-only source tree)
```
The harness starts a Ledger, creates the ledger **and its required `reference` index**, runs the functional suite (84 checks), the resilience/security suite (89), a graceful-shutdown check and six production-preflight refusals. In constrained sandboxes the Ledger may refuse writes on disk-usage health checks; pass `LEDGER_EXTRA_FLAGS="--health-wal-threshold 0.99 --health-data-threshold 0.99"`.

## Layout

```
treasury/          engine (ledger client, store, rails calendar, bank adapters, workflows, HTTP API), Dockerfile, tests/ (e2e, resilience, client)
gke/               gke-deploy.sh (prompts for cluster details; --render-only | --plan | --apply), terraform/, k8s/
scripts/           earlier building blocks: Formance CE install + e2e, Rafiki local bring-up + e2e, Rafiki->Ledger bridge, Hyperswitch lite (unbuilt)
```

## Bank connectivity: what is real and what is not

`treasury/banks.py` defines the `BankAdapter` interface (balance, submit, status, statement, healthy). **Only `MockBank` is implemented.** `BofACashProAdapter` and `NavyFederalAdapter` are stubs that raise until implemented against the interfaces those banks enable for your accounts (credentials, entitlements and API specs are required and were not available). The engine **refuses to start with mock banks** unless `TREASURY_ALLOW_MOCK_BANKS=1`, so simulated payments cannot be mistaken for real ones.

Rail windows and limits in `treasury/rails.py` are **defaults to confirm with each bank/operator** (override with `TREASURY_RAILS_JSON`, `TREASURY_HOLIDAYS`). Rail participation per bank (e.g. whether Navy Federal supports FedNow/RTP for business accounts) is an assumption in the default mock config.

## Known gaps before production

See [docs/PRODUCTION-READINESS.md](docs/PRODUCTION-READINESS.md) section 3-4. In short: real bank adapters and issuer-processor integration; a real GKE plan/apply (Terraform only validated offline); the container image build (Docker Hub rate-limited the base image in the sandbox); Ledger HA (single node); live Rafiki webhook signing; sanctions/AML; load/chaos/DR drills; independent security review. Hyperswitch (`scripts/hyperswitch-lite-up.sh`) was not brought up: its Rust build ran out of memory in the sandbox; the engine does not need it. Formance Enterprise (`ee/`) is not used (stays on MIT Community Edition).

## Safety notes

Secrets are never written to disk by `gke-deploy.sh`; it generates them into Secret Manager. `--apply` requires typing the project ID. Defaults in test scripts (e.g. `mysuperencryptionkey`, Rafiki dev secrets) are sandbox placeholders and must be replaced.
