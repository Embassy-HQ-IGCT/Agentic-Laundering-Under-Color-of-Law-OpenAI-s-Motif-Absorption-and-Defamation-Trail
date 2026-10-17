# Formance treasury stack

Open-source (Apache-2.0), ledger-first treasury, payments and card-authorization engine built on **Formance** (Ledger v3 over its HTTP API), with **Rafiki** (Interledger / Open Payments) in front and bank rails behind. Built to run on **GKE**.

> **Status: hardened and sandbox-verified; not yet production-approved.** The engine has crash-safe money flows, request-bound idempotency,
> signed-request auth with scopes, Postgres state, observability and graceful shutdown, verified by **231 automated checks** (SQLite and
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
| **Stripe Issuing** | Real-time authorization webhook (`/stripe/issuing/authorizations`) decides each swipe with a Ledger hold on the card's wallet; captures (hold first, wallet for over/force capture), refunds, refund reversals and authorization close-out from `/stripe/webhooks`. Stripe-Signature verified with the Stripe SDK; replay-safe on Stripe object ids. Works for virtual cards and Apple Pay / Google Pay tokens (the engine only ever sees `ic_` card ids). | tested (Stripe-documented payloads) |
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

## Open source and free - what that does and does not mean

- **This project's code is Apache-2.0** (`LICENSE`, `NOTICE`); contributions are welcome (`CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`). The engine uses only the Python standard library plus the PostgreSQL driver (psycopg, LGPL-3.0, used unmodified).
- **Free to run locally:** `make test` (SQLite), `make test-pg`, and the (untested-as-a-whole) `deploy/docker-compose.yml` need only a machine. Infrastructure-as-code works with **OpenTofu** (MPL-2.0, preferred) or Terraform (BUSL-1.1, source-available).
- **Not free of cost:** cloud resources (GKE, Cloud SQL), bank and card-network access, FX liquidity, and compliance work are paid, third-party matters. See `docs/COMPLIANCE.md`: this is software, not a bank, and operators must hold the licences their activity requires.
- **Unresolved licence gap:** Formance **Ledger v3 published no licence terms** in the revision this was tested against (`THIRD_PARTY.md`). The engine bundles none of its code, but you need written terms (or a licensed release, or a different ledger behind `treasury/ledger.py`) before production use or redistribution of the full stack. Formance Payments' Community Edition is MIT only if you have no registered Formance cloud account; its `ee/` code is separately licensed and not used here.

## Layout

```
treasury/          engine (ledger client, store, rails calendar, bank adapters, workflows, HTTP API), Dockerfile, tests/ (e2e, resilience, client)
gke/               gke-deploy.sh (prompts for cluster details; --render-only | --plan | --apply), terraform/, k8s/
scripts/           earlier building blocks: Formance CE install + e2e, Rafiki local bring-up + e2e, Rafiki->Ledger bridge, Hyperswitch lite (unbuilt)
```

## Bank connectivity: what is real and what is not

`treasury/banks.py` defines the `BankAdapter` interface (balance, submit, status, statement, healthy). **`MockBank` (tests) and `ManualBank` (operator-assisted live wires + statement ingest, see `deploy/hetzner/README.md`) are implemented.** `BofACashProAdapter` and `NavyFederalAdapter` are stubs that raise until implemented against the interfaces those banks enable for your accounts (credentials, entitlements and API specs are required and were not available). The engine **refuses to start with mock banks** unless `TREASURY_ALLOW_MOCK_BANKS=1`, so simulated payments cannot be mistaken for real ones.

Rail windows and limits in `treasury/rails.py` are **defaults to confirm with each bank/operator** (override with `TREASURY_RAILS_JSON`, `TREASURY_HOLIDAYS`). Rail participation per bank (e.g. whether Navy Federal supports FedNow/RTP for business accounts) is an assumption in the default mock config.

## Known gaps before production

See [docs/PRODUCTION-READINESS.md](docs/PRODUCTION-READINESS.md) section 3-4. In short: real bank adapters and issuer-processor integration; a real GKE plan/apply (Terraform only validated offline); the container image build (Docker Hub rate-limited the base image in the sandbox); Ledger HA (single node); live Rafiki webhook signing; sanctions/AML; load/chaos/DR drills; independent security review. Hyperswitch (`scripts/hyperswitch-lite-up.sh`) was not brought up: its Rust build ran out of memory in the sandbox; the engine does not need it. Formance Enterprise (`ee/`) is not used.

## Safety notes

Secrets are never written to disk by `gke-deploy.sh`; it generates them into Secret Manager. `--apply` requires typing the project ID. Defaults in test scripts (e.g. `mysuperencryptionkey`, Rafiki dev secrets) are sandbox placeholders and must be replaced.
