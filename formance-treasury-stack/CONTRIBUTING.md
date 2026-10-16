# Contributing

Thanks for helping. This code moves money, so the bar for changes is: **tested, idempotent, and boringly safe**.

## Setup
```bash
make ledger          # clones + builds the pinned Formance Ledger v3 into .build/ (needs git, Go >= 1.27)
make test            # 181 checks on SQLite   (functional, resilience/security, shutdown, production refusals)
make test-pg         # same suites on PostgreSQL (needs PG_URL)
make lint            # pyflakes + shellcheck
make audit           # renders the deploy kit and audits the Kubernetes hardening properties
```

## Rules for changes
- **Every money movement is one idempotent Ledger transaction with a deterministic reference.** Never add a second code path that moves value.
- Anything that can fail between two side effects (ledger / bank / database) needs a recovery path and a **fault-injection test**
  (`tests/resilience.py` shows how: arm `/admin/crash`, kill the request, recover, assert funds and bank instructions are exactly-once).
- New bank adapters must satisfy the contract in `treasury/banks.py` (idempotent `submit`, truthful `lookup`, honest `sent` semantics).
- Keep the engine on the Python standard library (+ the PostgreSQL driver). Justify any new dependency and add it to `THIRD_PARTY.md`.
- Add tests with the change; keep `make lint` clean. No secrets in code, tests or docs.
- Sign off your commits (`git commit -s`): you certify the Developer Certificate of Origin (https://developercertificate.org/) and that you
  may license the work under Apache-2.0.
