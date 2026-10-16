# Single-host live deployment (Hetzner / DigitalOcean) with operator-assisted BofA wires

One Docker host: Postgres, Formance Ledger v3, the treasury engine and Caddy (automatic TLS). No cloud vault: secrets are
encrypted in Git with sops+age and decrypted only into the process environment at start.

## 1. Stand it up (about 15 minutes)

```bash
# on the server: docker + compose plugin, plus sops and age.  A static IP is what you give the bank to allowlist.
git clone <your repo> && cd formance-treasury-stack/deploy/hetzner
export DOMAIN=treasury.example.com          # A record -> this server
./bootstrap.sh                               # creates keys.txt (age) and secrets.enc.env; COPY keys.txt OFF the server
./up.sh                                      # builds the Ledger at the pinned ref, bootstraps it (reference index), starts everything
curl -fsS https://$DOMAIN/readyz             # {"ledger":true,"db":true,...}
```
API clients sign requests (see `treasury/tests/client.py`); the key lives in `secrets.enc.env` (`sops -d`).

## 2. The BofA wire loop (no bank API needed)

Bank config is `BANKS_JSON` (default in `bootstrap.sh`): `{"name":"bofa","type":"manual","rails":["FEDWIRE","SWIFT"],"ccys":["USD"]}`.

1. **Instruction out.** `POST /payouts` reserves the funds in the Ledger and queues an instruction (status `accepted`).
   `GET /bank/bofa/instructions?status=accepted` lists what to send: destination details and `reference_text` = `PAY:<id>`.
2. **Operator sends** the wire in BofA online banking, typing `PAY:<id>` into the wire reference / "originator to beneficiary" text.
   Then `POST /bank/bofa/instructions/<id>/sent {"sent_ref":"<IMAD/OMAD or confirmation no.>"}`.
   If it cannot be sent: `POST .../<id>/failed {"reason":"..."}` (refused once marked sent) - the engine releases the funds.
3. **Statement in.** Download the account activity (CSV) or BAI2 from BofA and `POST /bank/bofa/statement {"format":"csv","content":"..."}`.
   A debit containing `PAY:<id>` with the exact amount settles the payout. Credits whose text contains `WALLET:<id>` are posted to
   that wallet (otherwise to `unapplied:bofa:USD`). The response includes the reconciliation report; anything unexplained
   (unmatched debit, amount mismatch) is listed and stored, never absorbed.
4. **CSV columns** default to `Date, Description, Amount, Running Bal.`; override in the bank config:
   `"csv": {"date":"Posted Date","desc":"Payee","debit":"Debit","credit":"Credit","balance":"Balance","date_fmt":"%m/%d/%Y"}`.
   Check one real download against this before the first run; the parser skips summary rows above the header.
5. The bank's available balance is taken from the latest statement; queued-but-undebited instructions are subtracted, so the
   router never queues more than the bank actually holds.

When CashPro API access is granted, replace the bank type with the API adapter; the Ledger, routing and reconciliation are unchanged.

## 3. Operations
- `./backup.sh` nightly (cron): Postgres dump + Ledger volume copy (brief Ledger stop), checksums. Ship off-box; rehearse `./restore.sh <dir>`.
- Admin access over WireGuard/Tailscale only; expose just 80/443.
- Upgrades: `git pull && ./up.sh`. Rotate by editing `sops secrets.enc.env` then `./up.sh`.
- Not covered here: Rafiki (run its own stack and point its webhooks at `/rafiki/webhooks`), HA (single node), sanctions/AML.
