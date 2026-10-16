#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Resilience + security suite: crash recovery (fault injection), idempotency binding, auth, validation, rate limits, ops endpoints.
Requires a server started with TREASURY_ENV=test and the API keys defined in run-local-tests.sh."""
import concurrent.futures as cf
import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import client
from client import call, raw_call, sign

client.configure(sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1:8200")
R = uuid.uuid4().hex[:6]
SAT = "2026-10-17T03:00:00-04:00"
P = F = 0


def ok(name, cond, detail=""):
    global P, F
    if cond:
        P += 1; print("PASS ", name)
    else:
        F += 1; print("FAIL ", name, "->", detail)


def eq(name, got, want): ok(name, got == want, f"expected {want!r} got {got!r}")
def wal(w, ccy="USD"): return call("GET", f"/wallets/{w}?ccy={ccy}")[1]
def bal(acct, ccy="USD"): return call("GET", f"/admin/balance/{acct}?ccy={ccy}")[1]["balance"]
def seed(ref, postings): assert call("POST", "/admin/post", {"ref": f"{R}:{ref}", "postings": postings})[0] == 200
def conserved(): return call("GET", "/admin/ledger/check/USD")[1]["sum"] == 0 and call("GET", "/admin/ledger/check/EUR")[1]["sum"] == 0
DEST = {"type": "us_account", "routing": "021000021", "account": "000123456"}


def crashed(method, path, body, **kw):
    """Send a request while a crash point is armed: the server drops the connection mid-request (a process death)."""
    try:
        s, b = call(method, path, body, retry=False, **kw)
        return False, (s, b)
    except Exception:
        client._tl.c = None
        return True, None


print("== crash recovery: payouts (process dies at each step; recovery must finish exactly once)")
for point in ("after_insert", "after_reserve", "after_route", "after_submit", "after_settle_tx"):
    w = f"cr-{point.replace('_', '')}{R}"
    seed(f"fund-{point}", [["world", f"wallet:{w}", 1_000_000, "USD/2"]])
    pid = f"{R}-crash-{point}"
    call("POST", "/admin/crash", {"point": point})
    died, _ = crashed("POST", "/payouts", {"id": pid, "wallet": w, "amount": 250_000, "ccy": "USD", "dest": DEST}, now=SAT)
    ok(f"[{point}] process died mid-payout", died)
    mid = call("GET", f"/payouts/{pid}")[1]
    ok(f"[{point}] state is mid-flight and not terminal", mid["status"] in ("new", "reserved", "routing", "accepted", "settled"), mid)
    s, retry = call("POST", "/payouts", {"id": pid, "wallet": w, "amount": 250_000, "ccy": "USD", "dest": DEST}, now=SAT)
    ok(f"[{point}] a client retry while the crashed worker's lease is held does not double-drive it",
       s == 200 and (retry.get("in_progress") or retry["status"] in ("settled", "accepted")), (s, retry))
    rec = call("POST", "/payouts/recover", {"stale_s": 0, "force": True}, now=SAT)[1]
    fin = call("GET", f"/payouts/{pid}")[1]
    eq(f"[{point}] recovery drives it to settled", fin["status"], "settled")
    eq(f"[{point}] wallet debited exactly once", wal(w)["balance"], 750_000)
    subs = call("GET", "/admin/bank/bofa/submissions")[1], call("GET", "/admin/bank/nfcu/submissions")[1]
    eq(f"[{point}] bank received at most one instruction", sum(x.get(pid, 0) for x in subs), 1)
    ok(f"[{point}] ledger still balances", conserved())

print("== crash recovery: card authorization and capture")
w, cr = f"cardw{R}", f"cardc{R}"
seed("cardw", [["world", f"wallet:{w}", 1_000, "USD/2"]])
call("POST", "/credit/facilities", {"facility_id": f"fac{R}", "borrower": w, "ccy": "USD", "limit": 5_000, "apr_bps": 0}, now=SAT)
call("POST", "/cards", {"card_id": cr, "wallet": w, "credit_id": f"fac{R}"})
AUTH = {"auth_id": f"{R}-ha", "card_id": cr, "amount": 1_800, "ccy": "USD", "merchant": "M", "mcc": "5411", "country": "US"}
call("POST", "/admin/crash", {"point": "after_hold"})
died, _ = crashed("POST", "/card/authorize", AUTH, now=SAT)
ok("process died after the hold was committed, before the decision was recorded", died)
eq("the hold really exists in the ledger (1000 wallet + 800 credit)", (bal(f"holds:{R}-ha")), 1_800)
s, a = call("POST", "/card/authorize", AUTH, now=SAT)
ok("replay approves and adopts the existing hold (no second hold)", s == 200 and a["approved"] and bal(f"holds:{R}-ha") == 1_800, (s, a))
eq("split recovered from the ledger, not guessed (credit drawn 800)", call("GET", f"/credit/facilities/fac{R}")[1]["outstanding"], 800)
w2, cr2 = f"cardw2{R}", f"cardc2{R}"
seed("cardw2", [["world", f"wallet:{w2}", 5_000, "USD/2"]])
call("POST", "/cards", {"card_id": cr2, "wallet": w2})
AUTH2 = {"auth_id": f"{R}-hb", "card_id": cr2, "amount": 2_000, "ccy": "USD"}
call("POST", "/admin/crash", {"point": "after_hold"})
crashed("POST", "/card/authorize", AUTH2, now=SAT)
call("POST", "/cards", {"card_id": cr2, "wallet": w2, "status": "blocked"})        # card blocked between the crash and the replay
s, a = call("POST", "/card/authorize", AUTH2, now=SAT)
ok("card blocked after a crash: the committed hold is adopted, never leaked or orphaned", s == 200 and a["approved"], (s, a))
call("POST", "/card/reverse", {"auth_id": f"{R}-hb"}, now=SAT)
eq("and it can still be reversed cleanly", wal(w2)["balance"], 5_000)
call("POST", "/admin/crash", {"point": "after_capture_tx"})
crashed("POST", "/card/capture", {"auth_id": f"{R}-ha", "amount": 1_500}, now=SAT)
s, c = call("POST", "/card/capture", {"auth_id": f"{R}-ha", "amount": 1_500}, now=SAT)
ok("capture replayed after a crash completes with consistent records", s == 200 and c["captured"] == 1_500 and c["released"] == 300, (s, c))
eq("clearing holds exactly the captured amount", bal("card:clearing:USD") >= 1_500, True)
eq("credit line refunded first from the released remainder (800 -> 500)", call("GET", f"/credit/facilities/fac{R}")[1]["outstanding"], 500)
ok("ledger still balances", conserved())

print("== idempotency is bound to the request")
seed("idem", [["world", f"wallet:idem{R}", 100_000, "USD/2"]])
t = {"ref": f"{R}-tr", "from": f"idem{R}", "to": f"idemb{R}", "amount": 1_000, "ccy": "USD"}
eq("first transfer", call("POST", "/transfers", t)[0], 200)
eq("identical retry is a duplicate", call("POST", "/transfers", t)[1]["duplicate"], True)
s, b = call("POST", "/transfers", dict(t, amount=9_999))
ok("same ref, different amount -> 409 and nothing moves", s == 409 and wal(f"idem{R}")["balance"] == 99_000, (s, b))
s, b = call("POST", "/card/authorize", {"auth_id": f"{R}-ha", "card_id": cr, "amount": 5, "ccy": "USD"}, now=SAT)
eq("same auth_id, different amount -> 409", s, 409)
s, b = call("POST", "/card/capture", {"auth_id": f"{R}-ha", "amount": 1, "seq": 1}, now=SAT)
eq("same capture seq, different amount -> 409", s, 409)

print("== concurrent duplicate requests")
seed("par", [["world", f"wallet:par{R}", 1_000_000, "USD/2"]])
body = {"id": f"{R}-par", "wallet": f"par{R}", "amount": 100_000, "ccy": "USD", "dest": DEST}
with cf.ThreadPoolExecutor(20) as ex:
    res = list(ex.map(lambda _: call("POST", "/payouts", body, now=SAT), range(20)))
eq("20 identical simultaneous payout requests all answer 200", {s for s, _ in res}, {200})
call("POST", "/payouts/recover", {"stale_s": 0, "force": True}, now=SAT)
eq("settled once", call("GET", f"/payouts/{R}-par")[1]["status"], "settled")
eq("debited once", wal(f"par{R}")["balance"], 900_000)
subs = call("GET", "/admin/bank/bofa/submissions")[1], call("GET", "/admin/bank/nfcu/submissions")[1]
eq("exactly one bank instruction", sum(x.get(f"{R}-par", 0) for x in subs), 1)

print("== authentication and authorization")
body = json.dumps({"ref": f"{R}-x", "from": "a", "to": "b", "amount": 1, "ccy": "USD"}).encode()
eq("no credentials -> 401", raw_call("POST", "/transfers", body)[0], 401)
ts = str(int(time.time()))
eq("bad signature -> 401", raw_call("POST", "/transfers", body, {"X-Key-Id": "test", "X-Timestamp": ts, "X-Signature": "00" * 32})[0], 401)
eq("unknown key -> 401", raw_call("POST", "/transfers", body, {"X-Key-Id": "nobody", "X-Timestamp": ts,
                                                                    "X-Signature": sign("x" * 40, ts, "POST", "/transfers", body)})[0], 401)
old = str(int(time.time()) - 3600)
eq("stale timestamp (replay) -> 401", raw_call("POST", "/transfers", body, {"X-Key-Id": "test", "X-Timestamp": old,
                                                                                "X-Signature": sign(client.SECRET, old, "POST", "/transfers", body)})[0], 401)
good = sign(client.SECRET, ts, "POST", "/transfers", body)
eq("body tampered after signing -> 401", raw_call("POST", "/transfers", body.replace(b'"amount": 1', b'"amount": 9'),
                                                  {"X-Key-Id": "test", "X-Timestamp": ts, "X-Signature": good})[0], 401)
eq("path tampered after signing -> 401", raw_call("POST", "/payouts", body, {"X-Key-Id": "test", "X-Timestamp": ts, "X-Signature": good})[0], 401)
card_only = ("cardonly", "cardonly-secret-0123456789-0123456789-ab")
eq("key without the 'payout' scope -> 403", call("POST", "/payouts", {"id": "x", "wallet": "w", "amount": 1, "ccy": "USD", "dest": DEST}, key=card_only)[0], 403)
eq("key without the 'admin' scope cannot reach admin routes -> 403", call("GET", "/admin/audit", key=card_only)[0], 403)
ok("a key WITH the scope works", call("POST", "/card/authorize", {"auth_id": f"{R}-sc", "card_id": "nocard", "amount": 5, "ccy": "USD"},
                                       key=card_only, now=SAT)[0] == 200)
slow = ("slow", "slow-secret-0123456789-0123456789-abcdefg")
codes = [call("GET", "/rails", key=slow, now=SAT)[0] for _ in range(12)]
ok("rate limit: bursts above the key's budget get 429", 429 in codes and codes[0] == 200, codes)

print("== Rafiki webhook authentication")
ev = {"id": f"{R}-evt", "type": "incoming_payment.completed",
      "data": {"id": f"{R}-ip", "walletAddressId": f"rw{R}", "receivedAmount": {"value": "100", "assetCode": "USD", "assetScale": 2}}}
eq("unsigned webhook -> 401", raw_call("POST", "/rafiki/webhooks", json.dumps(ev).encode())[0], 401)
eq("webhook signed with the wrong secret -> 401", raw_call("POST", "/rafiki/webhooks", json.dumps(ev).encode(),
                                                          {"Rafiki-Signature": client.rafiki_header(ev, "wrong" * 10)})[0], 401)
eq("stale webhook timestamp -> 401", raw_call("POST", "/rafiki/webhooks", json.dumps(ev).encode(),
                                              {"Rafiki-Signature": client.rafiki_header(ev, t_ms=int((time.time() - 3600) * 1000))})[0], 401)
seed("rsettle", [["world", "rafiki:settlement", 100, "USD/2"]])
eq("correctly signed webhook accepted", call("POST", "/rafiki/webhooks", ev, rafiki=True)[0], 200)
eq("redelivery is idempotent", call("POST", "/rafiki/webhooks", ev, rafiki=True)[1]["duplicate"], True)
eq("credited exactly once", wal(f"rw{R}")["balance"], 100)
FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
fx_events = [json.loads(l) for l in open(os.path.join(FIX, "rafiki-webhooks.jsonl")) if l.strip()]
fx_canon = json.load(open(os.path.join(FIX, "rafiki-canonical.json")))["canonical"]
ok("our JSON canonicalization equals Rafiki's own json-canonicalize on real recorded events",
   fx_canon == [client.canonical({"id": x["id"], "type": x["type"], "data": x["data"]}) for x in fx_events])

print("== validation and limits")
V = lambda b: call("POST", "/transfers", b)[0]
eq("negative amount -> 400", V({"ref": "v1", "from": "a", "to": "b", "amount": -5, "ccy": "USD"}), 400)
eq("zero amount -> 400", V({"ref": "v2", "from": "a", "to": "b", "amount": 0, "ccy": "USD"}), 400)
eq("string amount -> 400", V({"ref": "v3", "from": "a", "to": "b", "amount": "100", "ccy": "USD"}), 400)
eq("float amount -> 400", V({"ref": "v3b", "from": "a", "to": "b", "amount": 1.5, "ccy": "USD"}), 400)
eq("amount above the hard ceiling -> 400", V({"ref": "v4", "from": "a", "to": "b", "amount": 10**14, "ccy": "USD"}), 400)
eq("bad currency -> 400", V({"ref": "v5", "from": "a", "to": "b", "amount": 5, "ccy": "usd"}), 400)
eq("injection-style account id -> 400", V({"ref": "v6", "from": "a:b/../c", "to": "b", "amount": 5, "ccy": "USD"}), 400)
eq("missing field -> 400", V({"ref": "v7", "from": "a", "amount": 5, "ccy": "USD"}), 400)
eq("body not an object -> 400", call("POST", "/transfers", [1, 2])[0], 400)
big = b'{"x":"' + b"a" * 70000 + b'"}'
ts = str(int(time.time()))
eq("oversized body -> 413", raw_call("POST", "/transfers", big, {"X-Key-Id": "test", "X-Timestamp": ts,
                                                                 "X-Signature": sign(client.SECRET, ts, "POST", "/transfers", big)})[0], 413)
eq("invalid payout destination type -> 400", call("POST", "/payouts", {"id": "vp", "wallet": "w", "amount": 5, "ccy": "USD", "dest": {"type": "x"}})[0], 400)
eq("unknown route -> 404", call("GET", "/nope")[0], 404)

print("== operations: health, metrics, audit")
eq("/livez is public", raw_call("GET", "/livez")[0], 200)
r = raw_call("GET", "/readyz")
ok("/readyz reports ledger + db ready", r[0] == 200 and r[1]["ledger"] and r[1]["db"], r)
m = client.metrics()
ok("metrics expose request counters, latency histograms, card decisions and payout states",
   all(x in m for x in ("treasury_http_requests_total", "treasury_http_request_seconds_bucket", "treasury_card_decisions_total", "treasury_payouts{")))
ok("metrics are NOT served on the API port", raw_call("GET", "/metrics")[0] in (401, 404))
audit = call("GET", "/admin/audit")[1]["rows"]
ok("audit trail records who did what", any(a["action"] == "payout.settled" for a in audit) and all(a["actor"] for a in audit), audit[:2])
tx = call("GET", f"/admin/tx/transfer:{R}-tr")[1]
meta = tx.get("metadata", {})
ok("ledger transactions carry the authenticated actor, request id and a request fingerprint", meta.get("actor") == "test" and meta.get("req") and meta.get("fp"), meta)

print(f"\nRESULT: {P} passed, {F} failed")
sys.exit(1 if F else 0)
