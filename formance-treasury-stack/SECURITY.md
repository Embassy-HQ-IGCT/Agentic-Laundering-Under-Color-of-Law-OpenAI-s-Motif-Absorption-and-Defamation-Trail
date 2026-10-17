# Security policy

## Reporting a vulnerability
**Please do not open a public issue.** Use GitHub's *private vulnerability reporting* ("Report a vulnerability" under the repository's
Security tab). Include affected version/commit, impact, and reproduction steps. We aim to acknowledge within 3 business days.

## Scope
In scope: the treasury engine (`treasury/`), the deployment kit (`gke/`), and the harness scripts. Of particular interest: authentication or
scope bypass, replay of signed requests, idempotency or double-spend flaws, loss of funds on crash or retry, ledger-integrity breaks, secret
exposure, and unsafe defaults.

Out of scope: vulnerabilities in third-party components (report upstream), and issues requiring the test-only endpoints (`TREASURY_ENV=test`),
which production refuses to start with.

## Operating it safely
See `docs/PRODUCTION-READINESS.md` (controls, limitations, go-live checklist) and `docs/COMPLIANCE.md`.
Never commit credentials; production secrets belong in a secret manager (the deploy script generates them into Google Secret Manager).
