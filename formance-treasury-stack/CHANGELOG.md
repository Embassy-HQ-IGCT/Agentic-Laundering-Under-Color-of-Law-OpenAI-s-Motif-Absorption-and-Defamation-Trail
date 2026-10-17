# Changelog

## Unreleased
- Alerts: PrometheusRule (7 rules) + PodMonitor for the existing kube-prometheus-stack; promtool unit tests (`make alerts`).
- Fix: Stripe real-time authorization decisions are now counted in `treasury_card_decisions_total`.
- Stripe Issuing binding: real-time authorization webhook -> Ledger hold on the card's wallet; captures, refunds, refund reversals and
  close-out from the event webhook; Stripe SDK signature verification (`stripe==15.6.1`). 24 new checks.
- Test harness: one fresh PostgreSQL database per engine stage (the shared database made the manual-bank reconciliation see other
  suites' payouts). 231 checks pass on SQLite, PostgreSQL and the hardened run.
- Operator-assisted bank adapter (`type: manual`): live wires through a bank portal with no API - idempotent instruction queue,
  CSV/BAI2 statement ingest that settles payouts by `PAY:<id>`, operator sent/failed actions, reconciliation. 17 new checks (190 total).
- Single-host deploy kit (`deploy/hetzner`): Compose + Caddy TLS, sops/age secrets, backup/restore scripts.
- Initial open-source release: ledger-first treasury engine (card authorization, transfers, payouts with 24x7 rail routing, FX, credit,
  guarantees, sweeps, reconciliation, Rafiki intake), production hardening (crash-safe flows, signed requests, Postgres state,
  observability), GKE deployment kit, 181 automated checks.
