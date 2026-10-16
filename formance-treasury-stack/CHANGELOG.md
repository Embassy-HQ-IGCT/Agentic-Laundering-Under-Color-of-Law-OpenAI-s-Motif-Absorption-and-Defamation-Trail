# Changelog

## Unreleased
- Operator-assisted bank adapter (`type: manual`): live wires through a bank portal with no API - idempotent instruction queue,
  CSV/BAI2 statement ingest that settles payouts by `PAY:<id>`, operator sent/failed actions, reconciliation. 17 new checks (190 total).
- Single-host deploy kit (`deploy/hetzner`): Compose + Caddy TLS, sops/age secrets, backup/restore scripts.
- Initial open-source release: ledger-first treasury engine (card authorization, transfers, payouts with 24x7 rail routing, FX, credit,
  guarantees, sweeps, reconciliation, Rafiki intake), production hardening (crash-safe flows, signed requests, Postgres state,
  observability), GKE deployment kit, 181 automated checks.
