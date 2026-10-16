# Runbook: standing the stack up fast, and every blocker we hit

Scope: **Formance** Ledger v3 + Payments Community Edition, **Rafiki** (Interledger), the **treasury engine**, and the **GKE kit**. "Forminix" / "4minutes" in earlier notes = **Formance**.

## 1. How long it takes (measured, not estimated)

The scripted path is fast; the session that produced these scripts was not (exploration, three failed builds, and the blockers below). Timings from the sandbox, warm vs cold:

| Step | Command | Measured |
|---|---|---|
| Formance CE: clone + build Ledger and Payments (community build), start Postgres + Temporal, migrate, start server + worker, health-check | `scripts/install-formance-ce.sh <dir>` | **~3 min cold** (Go builds dominate); **26 s warm** |
| Formance CE end-to-end test (17 checks, mock PSP through Temporal) | `scripts/e2e-test.sh <dir>` | < 1 min |
| Rafiki: Node 24, `pnpm install`, build auth + backend, Postgres/Redis, start | `scripts/rafiki-up.sh <dir>` | Node 6 s + install 34 s + build 18 s + container start; **~1.5-2 min** |
| Treasury engine + Ledger, 181 automated checks (functional, resilience/security, shutdown, production refusals) | `treasury/run-local-tests.sh` | **~12 s** (prebuilt Ledger binary), same on SQLite and PostgreSQL |

**Realistic total on an unrestricted machine: roughly 5-6 minutes of compute (plus first-time image pulls), i.e. inside a 5-10 minute window.** Not included: GKE (never run against a real project), real bank adapters (stubs), Hyperswitch (not brought up).

## 2. Quickstart (clean Linux box: git, Go >= 1.27, Docker, Python >= 3.9, Node via npm)

```bash
export CONFIG_ENCRYPTION_KEY="$(openssl rand -hex 16)" PLUGIN_MAGIC_COOKIE="$(openssl rand -hex 8)"
bash scripts/install-formance-ce.sh ./work          # Ledger v3 + Payments CE + Temporal; prints the 16 CE connectors
bash scripts/e2e-test.sh ./work                     # optional: 17 checks incl. a mock PSP flowing through Temporal
LEDGER_BIN=./work/ledger-bin LEDGERCTL=./work/ledgerctl treasury/run-local-tests.sh    # engine: expect "ALL SUITES PASSED" (181 checks)
```
Rafiki is optional for the engine tests (webhooks are replayed from recorded events); run `scripts/rafiki-up.sh ./work` and `scripts/rafiki-e2e.cjs` (from `rafiki/packages/backend`) to exercise it live (14 checks).

## 3. Blockers hit and the remediation (so you do not rediscover them)

| # | Symptom | Cause | Remediation |
|---|---|---|---|
| 1 | `Cannot connect to the Docker daemon` | Daemon not running / stopped between tool sessions | `setsid nohup dockerd >dockerd.log 2>&1 </dev/null &`; `install-formance-ce.sh` now does this itself. A daemon restart stops all containers. |
| 2 | `docker compose up` fails pulling `ghcr.io/...`, `docker.juspay.io/...` (403 on `pkg-containers.githubusercontent.com`) | Sandbox egress proxy blocks those registries | Skip the ghcr gateway/console (`docker compose up -d postgres temporal temporal-ui`); Docker Hub images work (watch 429 rate limits). In an unrestricted environment the stock compose works. |
| 3 | Go-in-container build: `x509: certificate signed by unknown authority` | Proxy re-signs TLS; container lacks the CA | Build the Go binary on the host (`go build`) and run it natively against Postgres/Temporal containers. |
| 4 | Payments server dies: `failed reaching server: Frontend is not healthy yet` | Started before Temporal was ready (fixed `sleep` too short) | Wait for TCP 7233, then 10 s; poll `/_healthcheck`. In the script. |
| 5 | Worker: `bind: address already in use` (:9090, :8081) | Server's pprof port and Temporal UI already use them | `worker --listen :8082 --profiler-listen :9191`. |
| 6 | Ledger `WRITES_BLOCKED_DISK_FULL` | Default 80% disk health threshold trips on the sandbox's volume accounting | Sandbox only: `--health-wal-threshold 0.99 --health-data-threshold 0.99`. In production, size disks and keep the defaults. |
| 7 | Rafiki: `Expected version: 24, got v22` | Rafiki requires Node 24 | `npm install node@24` into a private dir and put its `.bin` first on `PATH`. |
| 8 | Rafiki auth: `ENOENT .../open-payments-specifications/openapi/auth-server.yaml` | Git submodule not initialised | `git submodule update --init --depth 1 open-payments-specifications`. |
| 9 | Rafiki: `invalid url` creating wallet address | Address must be `https://` **and** start with the tenant's `OPEN_PAYMENTS_URL` | Set `OPEN_PAYMENTS_URL=https://<host>` and pass plain names; fetch the wallet document over HTTP with a matching `Host` header locally. |
| 10 | Rafiki admin API rejects calls | Requests must be HMAC-signed: header `signature: t=<ms>, v1=<hmac_sha256(secret, "<t>.<canonical JSON body>")>` plus `tenant-id` | Use `json-canonicalize` (already a Rafiki dependency); see `scripts/rafiki-e2e.cjs`. |
| 11 | Rafiki payment stuck in FUNDING | The account-servicing entity must deposit sender liquidity after `outgoing_payment.created` | Handle that webhook (the e2e script does `depositOutgoingPaymentLiquidity`). |
| 12 | Hyperswitch router build killed (`SIGKILL`) | `rustc` peaked ~13.5 GB on a 15 GB machine (kernel OOM); build artifacts also used 13 GB disk | Not needed by the engine. Use prebuilt images on an unrestricted host, or a >= 32 GB build machine. Left un-brought-up. |
| 13 | `terraform init` fails (registry 403) | `registry.terraform.io` blocked | Download providers from `releases.hashicorp.com` into a filesystem mirror (`TF_CLI_CONFIG_FILE`). Not needed on an unrestricted machine. |
| 14 | `helm` download blocked (`get.helm.sh`); Interledger chart repo blocked | Proxy allow-list | `go install helm.sh/helm/v3/cmd/helm@v3.16.2`; Rafiki repo vendors older subcharts for offline checks. |
| 15 | Shell exits (code 144) after `pkill -f '<pattern>'` | The pattern matched the shell's own command line | Use `pkill -x <exact name>` or anchor the pattern (`'^python3 -I server.py'`). |
| 16 | Script hangs after printing output | `cd X && env ... &` backgrounds the whole chain; wrapper keeps the pipe open | `(cd X; env ... cmd >log 2>&1 </dev/null &)` with `;` and full redirects. |
| 17 | Card authorization ~52 ms vs Ledger ~11 ms | Nagle + delayed-ACK on split small writes | `TCP_NODELAY` on server and Ledger client; single write per response. Now ~11 ms p50 / 12 ms p95. |
| 18 | 60 parallel requests: `Connection reset` | Default listen backlog of 5 | `request_queue_size = 512`. |
| 19 | Integrity check showed a non-zero ledger sum | (a) `/accounts` pagination keys are top-level `next`/`hasMore`; (b) virtual `world` is not listed | Follow `next`; add `world` explicitly. Sum is now exactly 0. |
| 20 | `git push` 403: "Claude doesn't have GitHub access to <org>/<repo>" | The Claude GitHub App/token has no write access to that repository; repo creation is also not permitted for the integration | Reconnect GitHub at https://claude.ai/connect-github, install the Claude GitHub App on the target org/repo, or push from your own machine (see section 5). |
| 21 | Duplicate / crash recovery silently failed to find the original transaction | The Ledger's `reference=` query parameter is **ignored** (returns the latest page); the `filter` form fails with `INDEX_NOT_FOUND` unless a `reference` index exists | Create the index once per ledger: `ledgerctl indexes create --ledger <name> --type reference` (the harness and the K8s `ledger-bootstrap` Job do it); the engine refuses to start without it. For a known duplicate, fetch by the id in the 409 message. |
| 22 | `KeyError: 'id'` from a duplicate lookup, reported as HTTP 400 | `GET /transactions/{id}` nests the object under `data.transaction`; and the server mapped bare `KeyError` to 400, hiding the bug | Unwrap `data.transaction`; use a dedicated `NotFound` for unknown ids so genuine bugs surface as 500. |
| 23 | Crash tests "passed" without crashing | The test client auto-retried the dropped connection, re-running the request | `retry=False` for fault-injection calls. |
| 24 | Harness left a Ledger running; next run exited 22 | `kill $!` hit the wrapper subshell, not the Ledger | `exec` the Ledger inside the subshell; wait for it in the cleanup trap. |
| 25 | Harness exited 1 although every check passed | `set -e` inside the EXIT trap overrode the real status | Capture `$?` first, `set +e`, `exit $rc` at the end of the trap. |
| 26 | Hardened (uid 10001) run: engine never started | Harness temp dir was root-only | `chmod a+rx` the temp dir. |
| 27 | Container image could not be built in the sandbox | Docker Hub 429 on `python:3.12-slim`; and in-container `pip` is blocked by the proxy's TLS interception | Unverified here. In a normal environment `docker build treasury/` works; in a proxied one pre-download wheels and `pip install --no-index --find-links`. |

## 4. Licensing note

See `THIRD_PARTY.md`. In short: this project is Apache-2.0. Formance **Payments** Community Edition is MIT (only if you have no registered account on Formance's cloud); its `ee/` code needs an agreement for production. Formance **Ledger v3 shipped without any licence terms** in the tested revision: resolve that before production use or redistribution. Prefer OpenTofu (MPL-2.0) over Terraform (BUSL-1.1).

## 5. Getting this code onto GitHub

Pushing worked once the Claude GitHub App was authorised on the target repository (blocker 20). Creating a *new* repository is still not permitted for the integration, so create an empty public repo yourself and push there; and attaching `IGCT-Foreign-Authority/hyperswitch` with push access was accepted but reported `push_check: refused` (the Claude GitHub App has no write access there either). The fix is on the GitHub side: install/authorise the Claude GitHub App on the target repository. Simplest route from any machine with access:

```bash
git bundle create formance-treasury-stack.bundle claude/full-deployment-all-connectors-exi0oz   # or copy the directory
# elsewhere:  git init formance-treasury-stack && cd formance-treasury-stack && git pull <path-to>.bundle claude/full-deployment-all-connectors-exi0oz
#             git remote add origin git@github.com:<you>/formance-treasury-stack.git && git push -u origin HEAD
```

## 6. Still manual before production

Real bank adapters (credentials + API specs), authentication/mTLS on the engine API, HA for the Ledger and workflow state, sanctions/AML controls, a real GKE `--plan`/`--apply`, pinned image tags, and confirmation of each bank's rail participation and cutoff times.
