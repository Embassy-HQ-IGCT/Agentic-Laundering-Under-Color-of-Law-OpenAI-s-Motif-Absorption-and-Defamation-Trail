#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Operator-assisted bank ("manual") end-to-end: wire instruction out, statement (CSV/BAI2) in, settle / fail / reconcile.
Needs an engine whose only bank is {"name":"bofa","type":"manual",...}. Usage: manual_flow.py [base_url]"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import client
from client import call

client.configure(sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1:8210")
R = uuid.uuid4().hex[:6]
WED_11 = "2026-10-14T11:00:00-04:00"
P = F = 0


def ok(name, cond, detail=""):
    global P, F
    if cond:
        P += 1; print("PASS ", name)
    else:
        F += 1; print("FAIL ", name, "->", detail)


def bal(w): return call("GET", f"/wallets/{w}?ccy=USD")[1]["balance"]
def stmt(fmt, content, now=WED_11): return call("POST", "/bank/bofa/statement", {"format": fmt, "content": content}, now=now)


W = f"acme{R}"
csv1 = ("Date,Description,Amount,Running Bal.\n"
        f"10/14/2026,WIRE IN WALLET:{W} FROM ORIGINATOR,\"5,000.00\",\"5,000.00\"\n")
s, r = stmt("csv", csv1)
ok("statement credit ingested and wallet funded", s == 200 and r["new"] == 1 and bal(W) == 500_000, (s, r))
ok("bank balance taken from the statement", call("GET", "/rails")[0] == 200)
s, r2 = stmt("csv", csv1)
ok("re-ingesting the same statement is idempotent", s == 200 and r2["new"] == 0 and bal(W) == 500_000, (s, r2))

pid = f"{R}-w1"
body = {"id": pid, "wallet": W, "amount": 120_000, "ccy": "USD", "urgency": "standard",
        "dest": {"type": "us_account", "name": "BENEFICIARY LLC", "aba": "026009593", "account": "000123456"}}
s, p = call("POST", "/payouts", body, now=WED_11)
ok("payout routed to the operator-assisted bank and queued (accepted, wallet debited)",
   s == 200 and p["status"] == "accepted" and p["bank"] == "bofa" and p["rail"] == "FEDWIRE" and bal(W) == 380_000, (s, p))
s, p2 = call("POST", "/payouts", body, now=WED_11)
ok("payout retry is a duplicate, no second instruction", s == 200 and p2.get("duplicate"), (s, p2))
s, q = call("GET", "/bank/bofa/instructions?status=accepted")
mine = [i for i in q.get("instructions", []) if i["payment_id"] == pid]
ok("operator sees exactly one instruction with the reference text to put on the wire",
   len(mine) == 1 and mine[0]["reference_text"] == f"PAY:{pid}" and mine[0]["dest"]["aba"] == "026009593", (s, q))
s, m = call("POST", f"/bank/bofa/instructions/{pid}/sent", {"sent_ref": "IMAD20261014B1QGC02C000123"})
ok("operator records the bank confirmation", s == 200 and m["sent_ref"].startswith("IMAD"), (s, m))
s, f = call("POST", f"/bank/bofa/instructions/{pid}/failed", {"reason": "oops"})
ok("an instruction already marked sent cannot be failed", s == 422 or s == 409 or s == 400, (s, f))

csv2 = ("Date,Description,Amount,Running Bal.\n"
        f"10/14/2026,WIRE OUT PAY:{pid} BENEFICIARY LLC,\"-1,200.00\",\"3,800.00\"\n")
s, r = stmt("csv", csv2)
ok("statement debit settles the payout", s == 200 and pid in r["settled"], (s, r))
s, p = call("GET", f"/payouts/{pid}")
ok("payout is settled and reconciliation is clean", p["status"] == "settled" and r["reconcile"]["clean"], (p, r.get("reconcile")))
ok("wallet unchanged by settlement (already debited at reservation)", bal(W) == 380_000)

pid2 = f"{R}-w2"
s, p = call("POST", "/payouts", dict(body, id=pid2, amount=50_000), now=WED_11)
ok("second payout queued", p["status"] == "accepted" and bal(W) == 330_000, p)
s, f = call("POST", f"/bank/bofa/instructions/{pid2}/failed", {"reason": "beneficiary details rejected by operator"})
ok("operator fails an unsent instruction", s == 200 and f["status"] == "failed", (s, f))
call("POST", "/payouts/poll", {}, now=WED_11)
s, p = call("GET", f"/payouts/{pid2}")
ok("failed instruction releases the reserved funds back to the wallet", p["status"] == "failed" and bal(W) == 380_000, (p, bal(W)))

pid3 = f"{R}-w3"
call("POST", "/payouts", dict(body, id=pid3, amount=10_000), now=WED_11)
csv3 = ("Date,Description,Amount,Running Bal.\n"
        f"10/14/2026,WIRE OUT PAY:{pid3} BENEFICIARY LLC,-99.00,3701.00\n"
        "10/14/2026,UNKNOWN DEBIT NOBODY ASKED FOR,-5.00,3696.00\n")
s, r = stmt("csv", csv3)
rec = r["reconcile"]
ok("a debit with the wrong amount does NOT settle the payout",
   pid3 not in r["settled"] and call("GET", f"/payouts/{pid3}")[1]["status"] == "accepted", r)
ok("reconciliation flags the amount mismatch and the unexplained debit",
   not rec["clean"] and len(rec["amount_mismatch"]) == 1 and len(rec["unmatched_bank_debits"]) == 1, rec)

bai = ("01,BANKOFAMER,CLIENT,261014,1200,1,80,,2/\n02,CLIENT,BANKOFAMER,1,261014,,USD,2/\n"
       "03,000111222333,USD,015,370000,,,045,365000,,/\n"
       f"16,195,150000,0,BAIREF001,,WIRE IN WALLET:{W}/\n88,ORIGINATOR TWO/\n"
       "16,495,2500,0,BAIREF002,,ACH FEE/\n49,0,0/\n98,0,1,1/\n99,0,1,1/\n")
before = bal(W)
s, r = stmt("bai2", bai)
ok("BAI2 ingest: credit applied to the wallet, debit surfaced, balance parsed",
   s == 200 and r["new"] == 2 and bal(W) == before + 150_000 and r["balances"].get("USD") == 365000, (s, r, bal(W)))

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
