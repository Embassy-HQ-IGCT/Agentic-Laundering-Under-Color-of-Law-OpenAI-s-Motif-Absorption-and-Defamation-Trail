#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Card pre-authorization, transfers and the full hold lifecycle against an EXISTING ledger layout:
engine started with WALLET_ACCOUNT_PREFIX=liquidity:wallet: and WALLET_ALIASES_JSON={"corpus-6b":"treasury:available:corpus-6b"}.
Proves pre-auth debits the treasury balance account itself (not a copy). Usage: corpus_cards.py [base_url]"""
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import client
from client import call

client.configure(sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1:8220")
R = uuid.uuid4().hex[:6]
P = F = 0
TREAS, WAL = "treasury:available:corpus-6b", "liquidity:wallet:corpus-reflection"


def ok(name, cond, detail=""):
    global P, F
    if cond:
        P += 1; print("PASS ", name)
    else:
        F += 1; print("FAIL ", name, "->", detail)


def acct(a): return call("GET", f"/admin/balance/{a}")[1]["balance"]


s, _ = call("POST", "/admin/post", {"ref": f"{R}:seed", "postings": [["world", TREAS, 10_000_000, "USD/2"], ["world", WAL, 1_000_000, "USD/2"]]})
t0, w0 = acct(TREAS), acct(WAL)
ok("seeded the real treasury accounts", t0 >= 10_000_000 and w0 >= 1_000_000, (t0, w0))
ok("wallet view of the alias reads the treasury account", call("GET", "/wallets/corpus-6b?ccy=USD")[1]["balance"] == t0)
ok("wallet view of a prefixed id reads liquidity:wallet:<id>", call("GET", "/wallets/corpus-reflection?ccy=USD")[1]["balance"] == w0)

call("POST", "/cards", {"card_id": f"ct{R}", "wallet": "corpus-6b", "per_txn_limit": 5_000_000})
call("POST", "/cards", {"card_id": f"cw{R}", "wallet": "corpus-reflection", "per_txn_limit": 5_000_000})
A = lambda n, card, amt: call("POST", "/card/authorize", {"auth_id": f"{R}-a{n}", "card_id": card, "amount": amt, "ccy": "USD",
                                                            "merchant": "ACME", "mcc": "5411", "country": "US"})[1]
a1 = A(1, f"ct{R}", 250_000)
ok("pre-auth approved against the treasury balance", a1["approved"] and a1["response_code"] == "00", a1)
ok("the hold came out of the treasury account itself", acct(TREAS) == t0 - 250_000, (acct(TREAS), t0))
a2 = A(2, f"cw{R}", 100_000)
ok("pre-auth approved against the liquidity wallet", a2["approved"] and acct(WAL) == w0 - 100_000, a2)
ok("replay returns the same decision, no second hold", A(1, f"ct{R}", 250_000).get("duplicate") and acct(TREAS) == t0 - 250_000)
cap = call("POST", "/card/capture", {"auth_id": f"{R}-a1", "amount": 150_000})
ok("partial capture; the remainder returns to the treasury account", cap[0] == 200 and acct(TREAS) == t0 - 150_000, cap)
rv = call("POST", "/card/reverse", {"auth_id": f"{R}-a2"})
ok("reversal returns the full hold to the liquidity wallet", rv[0] == 200 and acct(WAL) == w0, rv)
big = A(3, f"cw{R}", 4_900_000)
ok("insufficient funds declines (51) without touching the account", not big["approved"] and big["response_code"] == "51" and acct(WAL) == w0, big)
tr = call("POST", "/transfers", {"ref": f"{R}-t", "from": "corpus-6b", "to": "corpus-reflection", "amount": 500_000, "ccy": "USD"})
ok("transfer between the treasury account and the wallet", tr[0] == 200 and acct(WAL) == w0 + 500_000 and acct(TREAS) == t0 - 150_000 - 500_000, tr)
print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
