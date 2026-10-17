# Third-party software and licences

This project's **own code is Apache-2.0** (see `LICENSE`). It talks to the components below over network APIs and does not copy their
source. "Verified" means the licence text or package metadata was read in the authoring environment; "Believed" means it is what the
upstream project is generally known to use but was **not** checked here - confirm before relying on it.

| Component | Used for | Licence | Status |
|---|---|---|---|
| **Formance Ledger v3** (`formancehq/ledger`, tested at `9a4bb29c8670e6f8c1a166bb85d31d26107f18a2`) | The money ledger (HTTP API) | **No licence file or statement found in that revision** (no LICENSE, no README mention, no module metadata) | Verified *absence*. **Unresolved - see below.** |
| Formance Payments (`formancehq/payments`) | Optional connector layer | MIT for the Community Edition, **conditioned on having no registered account on Formance's cloud offering**; anything under `ee/` is under a separate Enterprise licence (production use needs an agreement; dev/test is permitted) | Verified (`LICENSE`, `ee/LICENSE`) |
| Rafiki (`interledger/rafiki`) | Interledger / Open Payments | Apache-2.0 | Verified (`LICENSE`) |
| psycopg 3 | PostgreSQL driver (the only third-party Python dependency) | LGPL-3.0 | Verified (package metadata). Used unmodified as a library. |
| Temporal server | Workflow engine for Formance Payments | MIT | Believed |
| PostgreSQL | State store | PostgreSQL Licence (permissive) | Believed |
| OpenTofu | Infrastructure as code (preferred) | MPL-2.0 | Believed |
| HashiCorp Terraform | Infrastructure as code (alternative) | **BUSL-1.1 - source-available, not open source** | Believed. Prefer OpenTofu; the config validates with both. |
| Hyperswitch (`juspay/hyperswitch`) | Not used by the engine | Apache-2.0 | Verified |

## The Ledger v3 licensing gap (action required)

Without a licence, copyright law's default is **all rights reserved**: you have no stated permission to copy, modify or redistribute that
code, and "it is on GitHub" does not grant one. This project is unaffected as source (it contains none of it), but **running or
redistributing the full stack depends on it**. Before production use or redistribution, do one of:

1. Ask Formance for written licence terms for Ledger v3 (or check for a release/tag that states them).
2. Use a Ledger release that carries an explicit open-source licence.
3. Swap the ledger behind `treasury/ledger.py` (a thin REST client): the engine needs atomic multi-posting transactions, unique
   references, balance reads and a reference lookup, nothing exotic.

## Costs ("is it free?")

The software above is free to obtain and run. Running it is not free of cost: cloud resources (GKE, Cloud SQL, ...), bank and card-network
access, FX liquidity and compliance work are paid, third-party matters. The local path (`make test`, `deploy/docker-compose.yml`) needs only a
machine.
