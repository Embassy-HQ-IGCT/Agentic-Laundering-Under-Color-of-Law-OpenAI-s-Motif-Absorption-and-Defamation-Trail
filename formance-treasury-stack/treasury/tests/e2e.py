#!/usr/bin/env python3
"""End-to-end test of the treasury engine against a live engine + Ledger (server started with TREASURY_TEST_CLOCK=1 TREASURY_ADMIN=1).
Usage: e2e.py [base_url] [rafiki_webhooks.jsonl]"""
import concurrent.futures as cf
import http.client
import json
import os
import sys
import time
import uuid

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8200").replace("http://", "")
RAFIKI_LOG = sys.argv[2] if len(sys.argv) > 2 else ""
R = uuid.uuid4().hex[:6]            # run id: keeps ids unique across runs

SAT_3AM = "2026-10-17T03:00:00-04:00"      # Saturday, deep outside business hours
WED_11 = "2026-10-14T11:00:00-04:00"       # Wednesday, business hours
WED_14 = "2026-10-14T14:00:00-04:00"
FRI_20 = "2026-10-16T20:00:00-04:00"       # Friday after Fedwire close
SUN_22 = "2026-10-18T22:00:00-04:00"       # Sunday, Fedwire has reopened
MON_11 = "2026-10-19T11:00:00-04:00"

_tl = __import__("threading").local()


def call(method, path, body=None, now=None):
    c = getattr(_tl, "c", None)
    if c is None:
        c = http.client.HTTPConnection(BASE, timeout=30)
        c.connect()
        c.sock.setsockopt(__import__("socket").IPPROTO_TCP, __import__("socket").TCP_NODELAY, 1)
        _tl.c = c
    h = {"Content-Type": "application/json"}
    if now:
        h["X-Now"] = now
    c.request(method, path, json.dumps(body) if body is not None else None, h)
    r = c.getresponse()
    raw = r.read()
    return r.status, (json.loads(raw) if raw else {})


P, F = 0, 0


def ok(name, cond, detail=""):
    global P, F
    if cond:
        P += 1; print("PASS ", name)
    else:
        F += 1; print("FAIL ", name, "->", detail)


def eq(name, got, want):
    ok(name, got == want, f"expected {want!r} got {got!r}")


def wal(w, ccy="USD"):
    return call("GET", f"/wallets/{w}?ccy={ccy}")[1]


def seed(ref, postings):
    s, b = call("POST", "/admin/post", {"ref": f"{R}:{ref}", "postings": postings})
    assert s == 200, (s, b)


def fund_via_bank(bank, wallet, amount, ccy="USD", rail="FEDWIRE"):
    """Realistic funding: a credit appears at the bank, reconciliation posts it to the wallet."""
    call("POST", f"/admin/bank/{bank}/inbound", {"ccy": ccy, "amount": amount, "narrative": f"WIRE IN WALLET:{wallet}", "rail": rail})
    return call("POST", f"/recon/{bank}", {})[1]


print("== setup")
acme, bob = f"acme{R}", f"bob{R}"
seed("fx-usd", [["world", "fx:pool:USD", 500_000_000, "USD/2"]])
seed("fx-eur", [["world", "fx:pool:EUR", 500_000_000, "EUR/2"]])
rep = fund_via_bank("bofa", acme, 10_000_000)
eq("bank credit applied to wallet by reconciliation", [c["to"] for c in rep["credits_applied"]][-1:], [f"wallet:{acme}"])
eq("wallet funded", wal(acme)["balance"], 10_000_000)
rep2 = call("POST", "/recon/bofa", {})[1]
eq("re-running reconciliation does not double-credit", wal(acme)["balance"], 10_000_000)
ok("re-run reports credits as already posted", all(c["already_posted"] for c in rep2["credits_applied"]), rep2["credits_applied"])

print("== rail availability (America/New_York)")
m = call("GET", "/rails", now=SAT_3AM)[1]
ok("Sat 03:00: FedNow+RTP open & instant", m["FEDNOW"]["open"] and m["FEDNOW"]["instant"] and m["RTP"]["open"])
ok("Sat 03:00: Fedwire closed, ACH queued for next business day", not m["FEDWIRE"]["open"] and "next business day" in m["ACH"]["note"], m)
m = call("GET", "/rails", now=WED_11)[1]
ok("Wed 11:00: Fedwire open, ACH same-day window", m["FEDWIRE"]["open"] and "same-day" in m["ACH"]["note"], m)
ok("Fri 20:00: Fedwire closed (after 19:00 ET)", not call("GET", "/rails", now=FRI_20)[1]["FEDWIRE"]["open"])
ok("Sun 22:00: Fedwire reopened (21:00 ET Sunday)", call("GET", "/rails", now=SUN_22)[1]["FEDWIRE"]["open"])

print("== internal transfers")
s, t = call("POST", "/transfers", {"ref": f"{R}-t1", "from": acme, "to": bob, "amount": 250_000, "ccy": "USD"})
ok("instant wallet transfer", s == 200 and not t["duplicate"], (s, t))
s, t = call("POST", "/transfers", {"ref": f"{R}-t1", "from": acme, "to": bob, "amount": 250_000, "ccy": "USD"})
ok("retry is idempotent (no second debit)", s == 200 and t["duplicate"] and wal(bob)["balance"] == 250_000, (s, t))
s, t = call("POST", "/transfers", {"ref": f"{R}-t2", "from": bob, "to": acme, "amount": 9_999_999, "ccy": "USD"})
ok("overdraft transfer declined (51)", s == 422 and t["response_code"] == "51", (s, t))

print("== payouts outside standard hours")
before = wal(acme)["balance"]
s, p = call("POST", "/payouts", {"id": f"{R}-p1", "wallet": acme, "amount": 150_000, "ccy": "USD",
                                 "dest": {"type": "us_account", "routing": "021000021", "account": "000123456"}}, now=SAT_3AM)
ok("Sat 03:00 payout goes out instantly on FedNow", s == 200 and p["status"] == "settled" and p["rail"] == "FEDNOW", (s, p))
eq("wallet debited exactly once", wal(acme)["balance"], before - 150_000)
s, p2 = call("POST", "/payouts", {"id": f"{R}-p1", "wallet": acme, "amount": 150_000, "ccy": "USD", "dest": {}}, now=SAT_3AM)
ok("payout retry returns same payout, no second debit", p2["duplicate"] and wal(acme)["balance"] == before - 150_000, p2)

call("POST", "/admin/bank/bofa/control", {"down": True})
s, p = call("POST", "/payouts", {"id": f"{R}-p2", "wallet": acme, "amount": 100_000, "ccy": "USD",
                                 "dest": {"type": "us_account", "routing": "021000021", "account": "000123456"}}, now=SAT_3AM)
ok("bank reporting down is skipped by the router; payout goes via the second bank", p["bank"] == "nfcu" and p["status"] == "settled", p)
call("POST", "/admin/bank/bofa/control", {"down": False, "refuse_connect": True})
s, p = call("POST", "/payouts", {"id": f"{R}-p2b", "wallet": acme, "amount": 100_000, "ccy": "USD",
                                 "dest": {"type": "us_account", "routing": "021000021", "account": "000123456"}}, now=SAT_3AM)
ok("connection refused at submit (definitely not sent) -> safe failover, attempt logged",
   p["bank"] == "nfcu" and p["status"] == "settled" and p["attempts"][0]["result"] == "unreachable", p)
call("POST", "/admin/bank/bofa/control", {"refuse_connect": False, "timeout_after_send": True})
bw = wal(acme)["balance"]
s, p = call("POST", "/payouts", {"id": f"{R}-p2c", "wallet": acme, "amount": 100_000, "ccy": "USD",
                                 "dest": {"type": "us_account", "routing": "021000021", "account": "000123456"}}, now=SAT_3AM)
ok("timeout AFTER send (outcome unknown) -> parked for review, NOT retried elsewhere (no double-pay)",
   p["status"] == "review" and len(p["attempts"]) == 1 and p["attempts"][0]["result"] == "unknown", p)
eq("funds stay committed while the outcome is unknown", wal(acme)["balance"], bw - 100_000)
call("POST", "/admin/bank/bofa/control", {"timeout_after_send": False})
call("POST", "/admin/bank/nfcu/control", {"down": True}); call("POST", "/admin/bank/bofa/control", {"down": True})
b4 = wal(acme)["balance"]
s, p = call("POST", "/payouts", {"id": f"{R}-p3", "wallet": acme, "amount": 100_000, "ccy": "USD",
                                 "dest": {"type": "us_account", "routing": "021000021", "account": "000123456"}}, now=SAT_3AM)
ok("both banks down -> payout fails cleanly and funds are returned", p["status"] == "failed" and wal(acme)["balance"] == b4, p)
call("POST", "/admin/bank/bofa/control", {"down": False}); call("POST", "/admin/bank/nfcu/control", {"down": False})

call("POST", "/admin/bank/bofa/control", {"fail_next": ["rail maintenance"]})
call("POST", "/admin/bank/nfcu/control", {"fail_next": ["limit exceeded"]})
s, p = call("POST", "/payouts", {"id": f"{R}-p4", "wallet": acme, "amount": 100_000, "ccy": "USD",
                                 "dest": {"type": "us_account", "routing": "021000021", "account": "000123456"}}, now=SAT_3AM)
ok("rejections cascade across banks then rails (FedNow@bofa, FedNow@nfcu, then RTP@bofa)",
   p["status"] == "settled" and p["rail"] == "RTP" and [a["result"] for a in p["attempts"]] == ["rejected", "rejected", "settled"], p)

fund_via_bank("bofa", acme, 300_000_000)
b4 = wal(acme)["balance"]
s, p = call("POST", "/payouts", {"id": f"{R}-p5", "wallet": acme, "amount": 200_000_000, "ccy": "USD",
                                 "dest": {"type": "us_account", "routing": "021000021", "account": "000123456"}}, now=SAT_3AM)
ok("amount above instant-rail limit on Saturday queues on ACH for Monday", p["rail"] == "ACH" and p["status"] == "accepted"
   and p["eta"].startswith("2026-10-19"), p)
eq("queued ACH funds are committed in flight (not spendable)", wal(acme)["balance"], b4 - 200_000_000)
p5_wallet_before = wal(acme)["balance"]
adv = call("POST", "/payouts/poll", {}, now=MON_11)[1]["advanced"]
ok("Monday poll settles the ACH payout", any(a["id"] == f"{R}-p5" and a["status"] == "settled" for a in adv), adv)
eq("settled payout did not change the wallet again", wal(acme)["balance"], p5_wallet_before)

s, p = call("POST", "/payouts", {"id": f"{R}-p6", "wallet": acme, "amount": 100_000, "ccy": "USD", "urgency": "standard",
                                 "dest": {"type": "us_account", "routing": "021000021", "account": "000123456"}}, now=WED_11)
ok("standard-urgency business-hours payout uses cheapest rail (ACH)", p["rail"] == "ACH" and p["status"] == "accepted", p)
call("POST", "/payouts/poll", {}, now=WED_14)
eq("ACH settles after same-day window", call("GET", f"/payouts/{R}-p6")[1]["status"], "settled")

s, p = call("POST", "/payouts", {"id": f"{R}-p7", "wallet": bob, "amount": 999_999_999, "ccy": "USD", "dest": {"type": "us_account"}}, now=WED_11)
ok("payout beyond wallet funds declined, nothing sent", s == 422, (s, p))

print("== FX conversion and international payout")
s, q = call("POST", "/fx/quote", {"wallet": acme, "sell_ccy": "USD", "buy_ccy": "EUR", "sell_amount": 1_000_000}, now=SAT_3AM)
ok("quote locks a rate with spread", s == 200 and q["buy"] < int(1_000_000 / 1.0850) and q["expires_at"], q)
s, c = call("POST", "/fx/convert", {"quote_id": q["quote_id"]}, now=SAT_3AM)
ok("convert executes atomically at the locked rate", s == 200 and c["bought"] == q["buy"], (s, c))
eq("EUR wallet credited", wal(acme, "EUR")["balance"], q["buy"])
s, c2 = call("POST", "/fx/convert", {"quote_id": q["quote_id"]}, now=SAT_3AM)
ok("converting the same quote twice is a no-op", c2.get("duplicate") and wal(acme, "EUR")["balance"] == q["buy"], c2)
s, q2 = call("POST", "/fx/quote", {"wallet": acme, "sell_ccy": "USD", "buy_ccy": "EUR", "sell_amount": 1_000}, now=SAT_3AM)
s, c3 = call("POST", "/fx/convert", {"quote_id": q2["quote_id"]}, now="2026-10-17T03:05:00-04:00")
ok("expired quote refused", s == 422, (s, c3))
eq("FX exposure = pool inventory movement (short EUR, long USD)", [call("GET", "/fx/exposure")[1][k] < 500_000_000 for k in ("EUR",)] +
   [call("GET", "/fx/exposure")[1]["USD"] > 500_000_000], [True, True])
s, p = call("POST", "/payouts", {"id": f"{R}-p8", "wallet": acme, "amount": 500_000, "ccy": "EUR",
                                 "dest": {"type": "international", "bic": "DEUTDEFF", "iban": "DE89370400440532013000", "country": "DE"}}, now=SAT_3AM)
ok("international EUR payout over SWIFT accepted on a Saturday, value-dated Monday", p["rail"] == "SWIFT" and p["status"] == "accepted"
   and p["eta"].startswith("2026-10-19"), p)
call("POST", "/payouts/poll", {}, now=MON_11)
eq("SWIFT payout settles on value date", call("GET", f"/payouts/{R}-p8")[1]["status"], "settled")

print("== card pre-authorization from wallet availability")
call("POST", "/cards", {"card_id": f"c1{R}", "wallet": bob, "per_txn_limit": 500_000, "blocked_mcc": ["7995"], "allowed_countries": ["US", "CA"]})
A = lambda n, amt, **k: call("POST", "/card/authorize", dict({"auth_id": f"{R}-a{n}", "card_id": f"c1{R}", "amount": amt, "ccy": "USD",
                                                             "merchant": "ACME STORE", "mcc": "5411", "country": "US"}, **k), now=SAT_3AM)[1]
a1 = A(1, 100_000)
ok("approve when funds available (00)", a1["approved"] and a1["response_code"] == "00", a1)
w = wal(bob)
ok("hold reduces spendable immediately", w["balance"] == 150_000 and w["card_holds"] == 100_000, w)
a2 = A(2, 200_000)
ok("decline insufficient funds (51)", not a2["approved"] and a2["response_code"] == "51", a2)
ok("decline blocked merchant category (62)", A(3, 1_000, mcc="7995")["response_code"] == "62")
ok("decline restricted country (62)", A(4, 1_000, country="RU")["response_code"] == "62")
ok("decline over per-transaction limit (61)", A(5, 600_000)["response_code"] == "61")
a1b = A(1, 100_000)
ok("replayed authorization returns the same decision, no second hold", a1b["duplicate"] and a1b["approved"] and wal(bob)["card_holds"] == 100_000, a1b)
s, cap = call("POST", "/card/capture", {"auth_id": f"{R}-a1", "amount": 60_000}, now=SAT_3AM)
ok("capture 60.00 of 100.00 releases the remainder", s == 200 and cap["captured"] == 60_000 and cap["released"] == 40_000, cap)
eq("wallet restored for the released part", wal(bob)["balance"], 190_000)
s, cap2 = call("POST", "/card/capture", {"auth_id": f"{R}-a1", "amount": 10}, now=SAT_3AM)
ok("capturing a closed authorization refused", s == 422, (s, cap2))
a6 = A(6, 90_000); rv = call("POST", "/card/reverse", {"auth_id": f"{R}-a6"}, now=SAT_3AM)[1]
ok("reversal releases the full hold", rv["released"] == 90_000 and wal(bob)["balance"] == 190_000, rv)
a7 = A(7, 40_000)
exp = call("POST", "/card/expire", {}, now="2026-10-25T03:00:00-04:00")[1]["expired"]
ok("stale holds expire and release", any(e["auth_id"] == f"{R}-a7" for e in exp) and wal(bob)["balance"] == 190_000, exp)

print("== card: overspend race, latency, credit line")
race = f"race{R}"
seed("race", [["world", f"wallet:{race}", 1_000, "USD/2"]])
call("POST", "/cards", {"card_id": f"c2{R}", "wallet": race})
def race_auth(i):
    return call("POST", "/card/authorize", {"auth_id": f"{R}-r{i}", "card_id": f"c2{R}", "amount": 100, "ccy": "USD"}, now=SAT_3AM)[1]
with cf.ThreadPoolExecutor(60) as ex:
    res = list(ex.map(race_auth, range(60)))
approved = sum(1 for r in res if r["approved"])
eq("60 simultaneous 1.00 auths on a 10.00 wallet: exactly 10 approved", approved, 10)
eq("wallet fully held, never negative", (wal(race)["balance"], wal(race)["card_holds"]), (0, 1_000))
rich = f"rich{R}"
seed("rich", [["world", f"wallet:{rich}", 100_000_000, "USD/2"]])
call("POST", "/cards", {"card_id": f"c3{R}", "wallet": rich})
lat = []
for i in range(200):
    t0 = time.perf_counter()
    r = call("POST", "/card/authorize", {"auth_id": f"{R}-l{i}", "card_id": f"c3{R}", "amount": 100, "ccy": "USD"}, now=SAT_3AM)[1]
    lat.append((time.perf_counter() - t0) * 1000)
lat.sort()
print(f"INFO  card auth end-to-end latency over HTTP (200 sequential): p50={lat[100]:.1f}ms p95={lat[190]:.1f}ms p99={lat[198]:.1f}ms")
ok("authorization p95 under 250ms in this sandbox", lat[190] < 250, lat[190])
cr = f"cr{R}"
seed("cr", [["world", f"wallet:{cr}", 1_000, "USD/2"]])
call("POST", "/credit/facilities", {"facility_id": f"fcard{R}", "borrower": cr, "ccy": "USD", "limit": 5_000, "apr_bps": 0}, now=SAT_3AM)
call("POST", "/cards", {"card_id": f"c4{R}", "wallet": cr, "credit_id": f"fcard{R}"})
r = call("POST", "/card/authorize", {"auth_id": f"{R}-cr1", "card_id": f"c4{R}", "amount": 1_800, "ccy": "USD"}, now=SAT_3AM)[1]
ok("auth larger than wallet approved against credit line (wallet first, then credit)", r["approved"], r)
cs = call("GET", f"/credit/facilities/fcard{R}")[1]
eq("credit drawn only for the shortfall (800)", cs["outstanding"], 800)
r = call("POST", "/card/authorize", {"auth_id": f"{R}-cr2", "card_id": f"c4{R}", "amount": 9_000, "ccy": "USD"}, now=SAT_3AM)[1]
ok("auth beyond wallet+credit declined (51)", not r["approved"] and r["response_code"] == "51", r)
call("POST", "/card/reverse", {"auth_id": f"{R}-cr1"}, now=SAT_3AM)
eq("reversal returns the credit portion to the line and the rest to the wallet",
   (call("GET", f"/credit/facilities/fcard{R}")[1]["outstanding"], wal(cr)["balance"]), (0, 1_000))

print("== credit facilities, loans, guarantees")
call("POST", "/credit/facilities", {"facility_id": f"f{R}", "borrower": acme, "ccy": "USD", "limit": 1_000_000, "apr_bps": 1200}, now=WED_11)
w0 = wal(acme)["balance"]
d1 = call("POST", "/credit/draw", {"facility_id": f"f{R}", "ref": "d1", "amount": 400_000})[1]
ok("draw credits the borrower and records principal", d1["outstanding"] == 400_000 and wal(acme)["balance"] == w0 + 400_000, d1)
s, d2 = call("POST", "/credit/draw", {"facility_id": f"f{R}", "ref": "d2", "amount": 700_000})
ok("draw beyond the limit refused", s == 422, (s, d2))
ac = call("POST", "/credit/accrue", {"facility_id": f"f{R}", "date": "2026-10-15"}, now=WED_11)[1]
eq("interest accrues daily on principal (400000 * 12% / 365)", ac["interest"], 131)
ac2 = call("POST", "/credit/accrue", {"facility_id": f"f{R}", "date": "2026-10-15"}, now=WED_11)[1]
ok("accrual for the same date is idempotent", ac2["duplicate"] and ac2["outstanding"] == 400_131, ac2)
rp = call("POST", "/credit/repay", {"facility_id": f"f{R}", "ref": "r1", "amount": 9_999_999})[1]
ok("repay is capped at outstanding and clears the line", rp["repaid"] == 400_131 and rp["outstanding"] == 0, rp)
gi = call("POST", "/guarantees", {"guarantee_id": f"g{R}", "applicant": acme, "beneficiary": bob, "ccy": "USD", "amount": 200_000,
                                  "collateral_pct": 50}, now=WED_11)[1]
eq("guarantee issued with 50% cash collateral", gi["collateral_held"], 100_000)
bb = wal(bob)["balance"]; ab = wal(acme)["balance"]
gc = call("POST", "/guarantees/claim", {"guarantee_id": f"g{R}", "ref": "c1", "amount": 150_000})[1]
ok("claim pays collateral first, then the applicant's wallet", gc["paid"] == 150_000 and wal(bob)["balance"] == bb + 150_000
   and wal(acme)["balance"] == ab - 50_000 and gc["receivable_from_applicant"] == 0, gc)
s, g2 = call("POST", "/guarantees/claim", {"guarantee_id": f"g{R}", "ref": "c2", "amount": 100_000})
ok("claims beyond the guaranteed amount refused", s == 422, (s, g2))
poor = f"poor{R}"
call("POST", "/guarantees", {"guarantee_id": f"gu{R}", "applicant": poor, "beneficiary": bob, "ccy": "USD", "amount": 80_000,
                             "collateral_pct": 0}, now=WED_11)
gu = call("POST", "/guarantees/claim", {"guarantee_id": f"gu{R}", "ref": "c1", "amount": 80_000})[1]
eq("unsecured claim creates a receivable from the applicant", gu["receivable_from_applicant"], 80_000)
rl = call("POST", "/guarantees/release", {"guarantee_id": f"g{R}"})[1]
eq("release returns unused collateral", rl["status"], "released")

print("== sweeps and liquidity")
seed("tr", [["world", "treasury:operating:USD", 777_000, "USD/2"]])
sw = call("POST", "/sweeps", {"ref": f"{R}-s1", "from": "treasury:operating:USD", "to": "treasury:reserve:USD", "ccy": "USD",
                              "mode": "above", "target": 100_000})[1]
eq("sweep above target keeps the target balance", sw["swept"], 677_000)
sw = call("POST", "/sweeps", {"ref": f"{R}-s2", "from": "treasury:operating:USD", "to": "treasury:reserve:USD", "ccy": "USD"})[1]
eq("full sweep empties the pool (Numscript wildcard)", sw["swept"], 100_000)
sw = call("POST", "/sweeps", {"ref": f"{R}-s2", "from": "treasury:operating:USD", "to": "treasury:reserve:USD", "ccy": "USD"})[1]
ok("sweep retry is a no-op", sw["swept"] == 0 or sw.get("duplicate"), sw)
lq = call("POST", "/liquidity", {"targets": {"bofa:USD": 100_000_000, "nfcu:USD": 400_000_000}})[1]
ok("liquidity view suggests rebalancing bofa -> nfcu", lq["suggested_moves"] and lq["suggested_moves"][0]["to_bank"] == "nfcu", lq["suggested_moves"])

print("== reconciliation")
call("POST", "/admin/bank/bofa/inbound", {"ccy": "USD", "amount": 12_345, "narrative": "UNKNOWN REMITTER"})
call("POST", "/admin/bank/bofa/debit", {"ccy": "USD", "amount": 7_777, "narrative": "FEE ADJUSTMENT"})
rc = call("POST", "/recon/bofa", {})[1]
ok("matches settled payouts to statement debits", len(rc["matched"]) >= 3, rc["matched"])
ok("unattributable credit parked in suspense, not lost", any(c["to"] == "unapplied:bofa:USD" for c in rc["credits_unapplied"]), rc["credits_unapplied"])
ok("stray bank debit flagged, never absorbed", len(rc["unmatched_bank_debits"]) == 1 and not rc["clean"], rc["unmatched_bank_debits"])
rc2 = call("POST", "/recon/bofa", {})[1]
ok("re-run yields the same findings and posts nothing twice", len(rc2["unmatched_bank_debits"]) == 1 and
   all(c["already_posted"] for c in rc2["credits_applied"] + rc2["credits_unapplied"]), rc2)

print("== Rafiki (Interledger) events into the same ledger")
if RAFIKI_LOG and os.path.exists(RAFIKI_LOG):
    allev = [json.loads(l) for l in open(RAFIKI_LOG) if l.strip()]
    i = max((k for k, e in enumerate(allev) if e["type"] == "outgoing_payment.created"), default=None)
    evs = allev[max(0, i - 1):i + 3] if i is not None else []     # incoming.created, outgoing.created, outgoing.completed, incoming.completed
    types = {e["type"] for e in evs}
    if {"outgoing_payment.created", "outgoing_payment.completed", "incoming_payment.completed"} <= types:
        sender = next(e for e in evs if e["type"] == "outgoing_payment.created")["data"]["walletAddressId"]
        recv = next(e for e in evs if e["type"] == "incoming_payment.completed")["data"]["walletAddressId"]
        seed("rafiki", [["world", f"wallet:{sender}", 5_000, "USD/2"]])
        base_s, base_r = wal(sender)["balance"], wal(recv)["balance"]
        base_set = call("GET", "/admin/balance/rafiki:settlement")[1]["balance"]
        for e in evs:
            call("POST", "/rafiki/webhooks", e)
        for e in evs:                                   # Rafiki retries: must be harmless
            call("POST", "/rafiki/webhooks", e)
        eq("sender debited 15.00 once", wal(sender)["balance"], base_s - 1_500)
        eq("receiver credited 15.00 once", wal(recv)["balance"], base_r + 1_500)
        eq("Interledger settlement account nets to zero for an on-ledger payment", call("GET", "/admin/balance/rafiki:settlement")[1]["balance"], base_set)
    else:
        print("SKIP  recorded Rafiki log lacks a full payment cycle")
else:
    print("SKIP  no Rafiki webhook log supplied")

print("== ledger integrity")
for ccy in ("USD", "EUR"):
    chk = call("GET", f"/admin/ledger/check/{ccy}")[1]
    eq(f"{ccy}: sum of all balances is exactly zero (nothing created or lost)", chk["sum"], 0)

print(f"\nRESULT: {P} passed, {F} failed")
sys.exit(1 if F else 0)
