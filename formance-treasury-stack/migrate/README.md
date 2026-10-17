# Ledger v2 → v3 migration (native mirror)

Source: the existing Ledger v2 (`sovereign-book`, v2.2.58). Target: a clean pinned Ledger v3 (`treasury`).
History moves through `ledgerctl ledgers create --mode mirror`; cutover is `ledgerctl ledgers promote`. No backup restore,
no replay, no hand-written postings.

## Proven here (`e2e-mirror.sh`, against `ghcr.io/formancehq/ledger:v2.2.58`)

| Step | Result |
|---|---|
| v2.2.58 HTTP `/logs` with the mirror's `sort=id:asc` + `query={"$gt":{"id":N}}` | **ignored** (returns newest-first) → the HTTP source does not work against v2.2.58 |
| PostgreSQL source (`--mirror-source-type postgres`, DSN from `MIRROR_DSN`) | full history ingested; state `FOLLOWING` |
| `verify.py` source vs target | ids, postings, references, metadata, timestamps, reverts, account metadata, volumes, zero-sum: PASS |
| Live source write during `FOLLOWING` | followed and verified |
| Negative control (wrong crosswalk) | verification FAILS as it must |
| Client write to the mirror before promotion | refused (409) |
| Final sync (mirror cursor = source high-water log id), `promote` | `NORMAL`; target accepts writes |

Run it: `LEDGER_BIN=… LEDGERCTL=… migrate/e2e-mirror.sh` (docker, python3).

## Production cutover

Inputs by key name only: `MIRROR_DSN` (read-only role on the v2 Ledger PostgreSQL), the rewrite rules file (if any),
`CROSSWALK_FILE` (same mapping, for verification), `V2_BEARER_FILE` (Gateway token for verification reads).

1. **Create the mirror** on the v3 cluster leader (DSN from the environment, never argv):
   `MIRROR_DSN=… ledgerctl ledgers create treasury --mode mirror --mirror-source-type postgres --mirror-ledger-name sovereign-book --index reference --index account-asset [--mirror-rewrite-file rules.yaml]`
2. **Follow**: `ledgerctl ledgers get treasury` until `State: FOLLOWING` and `Cursor` = source high-water log id
   (`GET /v2/sovereign-book/logs?pageSize=1`).
3. **Verify**: `V2_URL=… V2_LEDGER=sovereign-book V3_URL=… V3_LEDGER=treasury EXPECT_REFERENCES=52a041a4-d27d-4612-9366-c962d7b69ef8-EXEC2 python3 migrate/verify.py` → `"result": "PASS"`.
4. **Write barrier**: stop every v2 writer (Payments, Wallets, Orchestration, connectors, scripts) under the authorised
   maintenance gate. The mirror keeps following until the source is quiet.
5. **Final sync**: repeat 2 and 3; the cursor must equal the source high-water log id taken *after* the barrier.
6. **Promote**: `ledgerctl ledgers promote treasury -y` → `Mode: NORMAL`. Promotion stops replication; it does not stop
   v2 writers — step 4 does.
7. **Switch writers** to the v3 `treasury` ledger; verify the first operations against the new ledger.

Abort before step 6 leaves v2 untouched and authoritative: `ledgerctl ledgers delete treasury` and start again.
