#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Stripe Issuing: real-time authorization webhook -> Ledger hold on the card's wallet; captures, refunds and authorization
close-out from the event webhook. Signatures computed per Stripe's documented scheme (t.payload, HMAC-SHA256, v1).
Usage: stripe_issuing.py [base_url]   env: STRIPE_ISSUING_AUTH_WEBHOOK_SECRET, STRIPE_WEBHOOK_SECRET"""
import hashlib
import hmac
import http.client
import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import client
from client import call

client.configure(sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1:8200")
AUTH_SECRET = os.environ["STRIPE_ISSUING_AUTH_WEBHOOK_SECRET"]
EV_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]
API_VERSION = "2025-03-31.basil"
R = uuid.uuid4().hex[:8]
P = F = 0


def ok(name, cond, detail=""):
    global P, F
    if cond:
        P += 1; print("PASS ", name)
    else:
        F += 1; print("FAIL ", name, "->", detail)


def post(path, ev, secret, t=None, sig=None):
    raw = json.dumps(ev).encode()
    t = t if t is not None else int(time.time())
    if sig is None:
        sig = hmac.new(secret.encode(), f"{t}.".encode() + raw, hashlib.sha256).hexdigest()
    host, port = client.BASE.split(":")
    c = http.client.HTTPConnection(host, int(port), timeout=10)
    c.request("POST", path, raw, {"Content-Type": "application/json", "Stripe-Signature": f"t={t},v1={sig}"})
    r = c.getresponse()
    data = r.read()
    out = (r.status, json.loads(data) if data else {}, r.getheader("Stripe-Version"))
    c.close()
    return out


def auth_req(aid, card, amount, mcc="5411"):
    return {"id": f"evt_{uuid.uuid4().hex[:12]}", "object": "event", "api_version": API_VERSION, "type": "issuing_authorization.request",
            "data": {"object": {"id": aid, "object": "issuing_authorization", "approved": False, "amount": 0, "currency": "usd",
                                "status": "pending", "card": {"id": card, "object": "issuing.card"},
                                "merchant_data": {"name": "ACME MARKET", "category_code": mcc, "country": "US"},
                                "pending_request": {"amount": amount, "currency": "usd", "merchant_amount": amount,
                                                    "merchant_currency": "usd", "is_amount_controllable": False}}}}


def txn(iid, card, typ, amount, aid=None):
    return {"id": f"evt_{uuid.uuid4().hex[:12]}", "object": "event", "api_version": API_VERSION, "type": "issuing_transaction.created",
            "data": {"object": {"id": iid, "object": "issuing.transaction", "type": typ, "amount": amount, "currency": "usd",
                                "authorization": aid, "card": card}}}


def auth_updated(aid, card, status):
    return {"id": f"evt_{uuid.uuid4().hex[:12]}", "object": "event", "api_version": API_VERSION, "type": "issuing_authorization.updated",
            "data": {"object": {"id": aid, "object": "issuing_authorization", "status": status, "card": {"id": card}}}}


def bal(a): return call("GET", f"/admin/balance/{a}")[1]["balance"]


AUTHP, EVP = "/stripe/issuing/authorizations", "/stripe/webhooks"
card, wid = f"ic_{R}", f"stripe-{R}"
W, CLR = f"wallet:{wid}", "card:clearing:USD"
call("POST", "/admin/post", {"ref": f"{R}:seed", "postings": [["world", W, 10_000, "USD/2"]]})
call("POST", "/cards", {"card_id": card, "wallet": wid})
clr0 = bal(CLR)

print("== signature")
s, b, _ = post(AUTHP, auth_req(f"iauth_{R}x", card, 100), AUTH_SECRET, sig="0" * 64)
ok("bad signature -> 401", s == 401, (s, b))
s, b, _ = post(AUTHP, auth_req(f"iauth_{R}x", card, 100), AUTH_SECRET, t=int(time.time()) - 900)
ok("stale timestamp -> 401", s == 401, (s, b))
s, b, _ = post(AUTHP, auth_req(f"iauth_{R}x", card, 100), EV_SECRET)
ok("event secret not accepted on the authorization endpoint -> 401", s == 401, (s, b))
s, b, _ = post(EVP, txn(f"ipi_{R}x", card, "capture", -100), AUTH_SECRET)
ok("authorization secret not accepted on the event endpoint -> 401", s == 401, (s, b))

print("== real-time authorization")
a1 = f"iauth_{R}1"
s, b, ver = post(AUTHP, auth_req(a1, card, 1_000), AUTH_SECRET)
ok("approve 10.00 -> 200 approved", s == 200 and b.get("approved") is True, (s, b))
ok("Stripe-Version echoed", ver == API_VERSION, ver)
ok("hold debited the wallet", bal(W) == 9_000 and bal(f"holds:{a1}") == 1_000, (bal(W), bal(f"holds:{a1}")))
s, b, _ = post(AUTHP, auth_req(a1, card, 1_000), AUTH_SECRET)
ok("replayed request approved without a second hold", s == 200 and b.get("approved") is True and bal(W) == 9_000, (b, bal(W)))
s, b, _ = post(AUTHP, auth_req(f"iauth_{R}2", card, 50_000), AUTH_SECRET)
ok("over-balance request declined", s == 200 and b.get("approved") is False and bal(W) == 9_000, (b, bal(W)))
s, b, _ = post(AUTHP, auth_req(f"iauth_{R}3", f"ic_unknown{R}", 100), AUTH_SECRET)
ok("unknown card declined", s == 200 and b.get("approved") is False, b)
s, b, _ = post(AUTHP, auth_req(f"iauth_{R}4", card, 0), AUTH_SECRET)
ok("zero-amount verification on an active card approved, no hold", s == 200 and b.get("approved") is True and bal(W) == 9_000, (b, bal(W)))
s, b, _ = post(AUTHP, auth_req(f"iauth_{R}5", f"ic_unknown{R}", 0), AUTH_SECRET)
ok("zero-amount verification on an unknown card declined", s == 200 and b.get("approved") is False, b)

print("== capture, close-out, refunds")
s, b, _ = post(EVP, txn(f"ipi_{R}1", card, "capture", -600, a1), EV_SECRET)
ok("capture 6.00 from the hold", s == 200 and b.get("from_hold") == 600 and bal(CLR) - clr0 == 600 and bal(f"holds:{a1}") == 400, (s, b))
s, b, _ = post(EVP, txn(f"ipi_{R}1", card, "capture", -600, a1), EV_SECRET)
ok("redelivered capture is a no-op", s == 200 and bal(CLR) - clr0 == 600 and bal(W) == 9_000, (s, b, bal(CLR) - clr0))
s, b, _ = post(EVP, auth_updated(a1, card, "closed"), EV_SECRET)
ok("authorization closed releases the remaining 4.00", s == 200 and bal(f"holds:{a1}") == 0 and bal(W) == 9_400, (s, b, bal(W)))
s, b, _ = post(EVP, auth_updated(a1, card, "closed"), EV_SECRET)
ok("redelivered close is a no-op", s == 200 and bal(W) == 9_400, (s, b, bal(W)))
s, b, _ = post(EVP, txn(f"ipi_{R}2", card, "capture", -150, a1), EV_SECRET)
ok("capture after close is taken from the wallet", s == 200 and b.get("from_hold") == 0 and bal(W) == 9_250 and bal(CLR) - clr0 == 750,
   (s, b, bal(W)))
s, b, _ = post(EVP, txn(f"ipi_{R}3", card, "capture", -250, None), EV_SECRET)
ok("force capture without authorization taken from the wallet", s == 200 and bal(W) == 9_000 and bal(CLR) - clr0 == 1_000, (s, b, bal(W)))
s, b, _ = post(EVP, txn(f"ipi_{R}4", card, "refund", 200, None), EV_SECRET)
ok("refund credits the wallet", s == 200 and bal(W) == 9_200 and bal(CLR) - clr0 == 800, (s, b, bal(W)))
s, b, _ = post(EVP, txn(f"ipi_{R}5", card, "refund", -200, None), EV_SECRET)
ok("refund reversal debits the wallet", s == 200 and bal(W) == 9_000 and bal(CLR) - clr0 == 1_000, (s, b, bal(W)))
s, b, _ = post(EVP, txn(f"ipi_{R}6", card, "capture", -1_000_000, None), EV_SECRET)
ok("capture beyond the wallet is refused (422) and posts nothing", s == 422 and bal(W) == 9_000, (s, b, bal(W)))
s, b, _ = post(EVP, txn(f"ipi_{R}7", f"ic_unknown{R}", "capture", -100, None), EV_SECRET)
ok("transaction for an unknown card -> 404", s == 404, (s, b))
s, b, _ = post(EVP, {"id": "evt_x", "object": "event", "api_version": API_VERSION, "type": "issuing_card.updated",
                     "data": {"object": {"id": card}}}, EV_SECRET)
ok("unrelated event acknowledged and ignored", s == 200 and b.get("ignored") == "issuing_card.updated", (s, b))

s, b = call("GET", "/admin/ledger/check/USD")
ok("ledger sums to zero", s == 200 and b.get("sum") == 0, b)
print(f"\nRESULT: {P} passed, {F} failed")
sys.exit(1 if F else 0)
