"""Treasury engine: the Ledger is the source of truth for every balance; SQLite holds only operational state
(payout/auth records, card controls, quotes, facility terms) needed to drive workflows and answer queries.

Chart of accounts (all amounts integer minor units, asset "<CCY>/2"):
  wallet:<id>                    holder funds (liability). Card holds, payouts, FX and credit draws all move value from/to here.
  holds:<auth_id>                card authorization holds
  card:clearing:<ccy>            captured card spend awaiting settlement with the network/issuer processor
  payout:<id>                    funds reserved for one outbound payment
  inflight:<bank>:<ccy>          funds committed to a bank instruction that has not settled yet
  unapplied:<bank>:<ccy>         inbound bank credits that could not be attributed to a wallet
  fx:pool:<ccy>                  FX inventory; its balances ARE the FX exposure
  credit:<facility>              credit line; negative balance = drawn principal
  income:interest:<ccy>          interest income
  collateral:<guarantee>         cash collateral for a guarantee
  receivable:guarantee:<id>      amount an applicant owes after an under-collateralised claim
  rafiki:inflight:<payment>, rafiki:settlement   Interledger payment legs
  treasury:<name>:<ccy>          treasury pools (operating, reserve, ...)
  world                          the outside (bank cash entering/leaving the institution)
"""
import datetime as dt
import json
import sqlite3
import threading
import time
import uuid

import rails as railmod
from banks import AdapterError, Rejected
from ledger import InsufficientFunds, Ledger, LedgerError

RESP = {"APPROVED": "00", "DO_NOT_HONOR": "05", "INSUFFICIENT_FUNDS": "51", "EXCEEDS_LIMIT": "61", "RESTRICTED": "62",
        "INVALID_CARD": "14", "SYSTEM_ERROR": "96"}


class Declined(Exception):
    def __init__(self, reason, code=None):
        super().__init__(reason)
        self.reason, self.code = reason, code


def asset(ccy):
    return f"{ccy}/2"


def iso(d):
    return d.isoformat() if isinstance(d, dt.datetime) else d


NS_ORDERED_DRAW = """vars {
  account $wallet
  account $credit
  account $dst
  monetary $amt
  monetary $limit
}
send $amt (
  source = {
    $wallet
    $credit allowing overdraft up to $limit
  }
  destination = $dst
)"""

NS_CREDIT_DRAW = """vars {
  account $credit
  account $wallet
  monetary $amt
  monetary $limit
}
send $amt (
  source = $credit allowing overdraft up to $limit
  destination = $wallet
)"""

NS_SWEEP_ALL = """vars {
  account $src
  account $dst
  asset $ccy
}
send [$ccy *] (
  source = $src
  destination = $dst
)"""

NS_INTEREST = """vars {
  account $credit
  account $income
  monetary $amt
}
send $amt (
  source = $credit allowing unbounded overdraft
  destination = $income
)"""

NS_GUARANTEE_CLAIM = """vars {
  account $collateral
  account $applicant
  account $receivable
  account $beneficiary
  monetary $amt
}
send $amt (
  source = {
    $collateral
    $applicant
    $receivable allowing unbounded overdraft
  }
  destination = $beneficiary
)"""

NS_MEMO = """vars {
  account $issued
  account $memo
  monetary $amt
}
send $amt (
  source = $issued allowing unbounded overdraft
  destination = $memo
)"""

SCHEMA = """
create table if not exists payouts(id text primary key, wallet text, amount integer, ccy text, dest text, urgency text,
  status text, rail text, bank text, bank_ref text, eta text, attempts text, reason text, created text, updated text);
create table if not exists cards(card_id text primary key, wallet text, status text, per_txn_limit integer, daily_limit integer,
  blocked_mcc text, allowed_countries text, credit_id text);
create table if not exists auths(auth_id text primary key, card_id text, wallet text, amount integer, ccy text, merchant text,
  mcc text, country text, status text, held_wallet integer, held_credit integer, credit_id text, captured integer default 0,
  released integer default 0, created text, expires text, response text);
create table if not exists quotes(id text primary key, wallet text, sell_ccy text, buy_ccy text, sell integer, buy integer,
  rate real, expires text, status text);
create table if not exists facilities(id text primary key, borrower text, ccy text, limit_minor integer, apr_bps integer,
  status text, last_accrual text);
create table if not exists guarantees(id text primary key, applicant text, beneficiary text, ccy text, amount integer,
  collateral integer, claimed integer default 0, status text, expiry text);
create table if not exists recon(bank text, bank_ref text, kind text, status text, detail text, primary key(bank, bank_ref));
"""


class Treasury:
    def __init__(self, ledger, banks, db_path=":memory:", fx_rates=None, fx_spread_bps=40, auth_ttl_h=168, quote_ttl_s=30):
        self.led, self.banks = ledger, banks
        self.db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()
        self.rails, self.hol = railmod.load_rails(), railmod.holidays()
        self.fx_mid = fx_rates or {"EURUSD": 1.0850, "GBPUSD": 1.2700}
        self.fx_spread_bps, self.auth_ttl_h, self.quote_ttl_s = fx_spread_bps, auth_ttl_h, quote_ttl_s

    # ------------------------------------------------------------------------------------------- helpers
    def q(self, sql, *a):
        with self.lock:
            return self.db.execute(sql, a).fetchall()

    def q1(self, sql, *a):
        r = self.q(sql, *a)
        return dict(r[0]) if r else None

    def x(self, sql, *a):
        with self.lock:
            self.db.execute(sql, a)

    @staticmethod
    def _need(d, *keys):
        for k in keys:
            if d.get(k) in (None, ""):
                raise ValueError(f"missing field: {k}")

    # ------------------------------------------------------------------------------------------- wallets & transfers
    def wallet(self, wid, ccy="USD"):
        a = asset(ccy)
        bal = self.led.balance(f"wallet:{wid}", a)
        held = sum(r["held_wallet"] + r["held_credit"] - r["captured"] - r["released"]
                   for r in self.q("select * from auths where wallet=? and ccy=? and status='authorized'", wid, ccy))
        fac = self.q1("select * from facilities where borrower=? and ccy=? and status='active'", wid, ccy)
        credit_avail = 0
        if fac:
            credit_avail = max(0, fac["limit_minor"] + self.led.balance(f"credit:{fac['id']}", a))
        return {"wallet": wid, "ccy": ccy, "balance": bal, "card_holds": held, "credit_available": credit_avail,
                "spendable": bal + credit_avail}

    def transfer(self, d):
        self._need(d, "ref", "from", "to", "amount", "ccy")
        if int(d["amount"]) <= 0:
            raise ValueError("amount must be positive")
        try:
            r = self.led.tx(f"transfer:{d['ref']}", [(f"wallet:{d['from']}", f"wallet:{d['to']}", int(d["amount"]), asset(d["ccy"]))],
                            metadata={"type": "transfer"})
        except InsufficientFunds:
            raise Declined("insufficient funds", "51")
        return {"status": "completed", "ledger_tx": r["id"], "duplicate": r["dup"]}

    # ------------------------------------------------------------------------------------------- routing & payouts
    def route_plan(self, amount, ccy, dest, urgency, now):
        """Ordered candidate routes [{bank, rail, instant, eta, fee}] honouring rail windows, limits, bank support and liquidity."""
        intl = dest.get("type") == "international" or ccy != "USD"
        if intl:
            order = ["SWIFT"]
        elif urgency == "instant":
            order = ["FEDNOW", "RTP", "FEDWIRE", "ACH"]
        else:
            order = ["ACH", "FEDWIRE", "FEDNOW", "RTP"]
        out = []
        for rank, rail in enumerate(order):
            spec = self.rails[rail]
            if amount > spec["max_minor"] or (spec["ccy"] != ["*"] and ccy not in spec["ccy"]):
                continue
            av = railmod.availability(rail, now, self.rails, self.hol)
            if not av["open"]:
                continue
            for bank in self.banks.values():
                if rail not in bank.rails or (ccy not in bank.ccys and "*" not in bank.ccys) or not bank.healthy():
                    continue
                try:
                    liq = bank.balance(ccy)
                except AdapterError:
                    continue
                if liq < amount:
                    continue
                out.append({"bank": bank.name, "rail": rail, "instant": av["instant"], "eta": iso(av["eta"]),
                            "fee": spec["fee_minor"], "liquidity": liq, "rank": rank})
        # rail preference first (speed for instant / cost for standard), then most liquid bank to keep both banks funded
        out.sort(key=lambda c: (c["rank"], -c["liquidity"]))
        return out

    def payout(self, d, now):
        self._need(d, "id", "wallet", "amount", "ccy", "dest")
        existing = self.q1("select * from payouts where id=?", d["id"])
        if existing:
            return self._payout_view(existing, duplicate=True)
        amount, ccy, wid, pid = int(d["amount"]), d["ccy"], d["wallet"], d["id"]
        if amount <= 0:
            raise ValueError("amount must be positive")
        urgency = d.get("urgency", "instant")
        try:
            self.led.tx(f"payout:{pid}:reserve", [(f"wallet:{wid}", f"payout:{pid}", amount, asset(ccy))], metadata={"type": "payout"})
        except InsufficientFunds:
            raise Declined("insufficient funds", "51")
        ts = iso(now)
        self.x("insert into payouts values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", pid, wid, amount, ccy, json.dumps(d["dest"]), urgency,
               "routing", None, None, None, None, "[]", None, ts, ts)
        plan = self.route_plan(amount, ccy, d["dest"], urgency, now)
        attempts, final = [], None
        if not plan:
            return self._fail_payout(pid, wid, amount, ccy, "no_route", attempts, now)
        for k, c in enumerate(plan):
            bank = self.banks[c["bank"]]
            fl = f"inflight:{c['bank']}:{ccy}"
            self.led.tx(f"payout:{pid}:route:{k}", [(f"payout:{pid}", fl, amount, asset(ccy))],
                        metadata={"type": "payout_route", "bank": c["bank"], "rail": c["rail"]})
            try:
                res = bank.submit(c["rail"], {"id": pid, "amount": amount, "ccy": ccy, "dest": d["dest"]}, now)
            except Rejected as e:
                self.led.tx(f"payout:{pid}:unroute:{k}", [(fl, f"payout:{pid}", amount, asset(ccy))], metadata={"type": "payout_unroute"})
                attempts.append({"bank": c["bank"], "rail": c["rail"], "result": "rejected", "reason": e.reason})
                continue
            except AdapterError as e:
                if e.sent is False:   # definitely not sent: safe to fail over
                    self.led.tx(f"payout:{pid}:unroute:{k}", [(fl, f"payout:{pid}", amount, asset(ccy))], metadata={"type": "payout_unroute"})
                    attempts.append({"bank": c["bank"], "rail": c["rail"], "result": "unreachable", "reason": str(e)})
                    continue
                # outcome unknown: funds stay committed in flight; do NOT try another bank (double-pay risk)
                attempts.append({"bank": c["bank"], "rail": c["rail"], "result": "unknown", "reason": str(e)})
                self._update_payout(pid, status="review", bank=c["bank"], rail=c["rail"], attempts=attempts,
                                    reason="bank outcome unknown; reconcile before retry", now=now)
                return self._payout_view(self.q1("select * from payouts where id=?", pid))
            attempts.append({"bank": c["bank"], "rail": c["rail"], "result": res["status"], "bank_ref": res["bank_ref"]})
            self._update_payout(pid, status=res["status"], bank=c["bank"], rail=c["rail"], bank_ref=res["bank_ref"],
                                eta=iso(res["eta"]), attempts=attempts, now=now)
            if res["status"] == "settled":
                self._settle_payout(pid, now)
            return self._payout_view(self.q1("select * from payouts where id=?", pid))
        return self._fail_payout(pid, wid, amount, ccy, "all_routes_failed", attempts, now)

    def _fail_payout(self, pid, wid, amount, ccy, reason, attempts, now):
        self.led.tx(f"payout:{pid}:release", [(f"payout:{pid}", f"wallet:{wid}", amount, asset(ccy))], metadata={"type": "payout_release"})
        self._update_payout(pid, status="failed", attempts=attempts, reason=reason, now=now)
        return self._payout_view(self.q1("select * from payouts where id=?", pid))

    def _settle_payout(self, pid, now):
        p = self.q1("select * from payouts where id=?", pid)
        self.led.tx(f"payout:{pid}:settle", [(f"inflight:{p['bank']}:{p['ccy']}", "world", p["amount"], asset(p["ccy"]))],
                    metadata={"type": "payout_settle", "bank": p["bank"], "bank_ref": p["bank_ref"], "rail": p["rail"]})
        self._update_payout(pid, status="settled", now=now)

    def poll_payouts(self, now):
        """Advance deferred payouts (ACH/Fedwire/SWIFT): settle those the bank reports settled, refund those it failed."""
        done = []
        for p in self.q("select * from payouts where status='accepted'"):
            p = dict(p)
            st = self.banks[p["bank"]].status(p["bank_ref"], now)
            if st == "settled":
                self._settle_payout(p["id"], now)
            elif st == "failed":
                fl = f"inflight:{p['bank']}:{p['ccy']}"
                self.led.tx(f"payout:{p['id']}:bankfail", [(fl, f"wallet:{p['wallet']}", p["amount"], asset(p["ccy"]))])
                self._update_payout(p["id"], status="failed", reason="bank reported failure", now=now)
            done.append({"id": p["id"], "status": self.q1("select status from payouts where id=?", p["id"])["status"]})
        return done

    def _update_payout(self, pid, now, **kw):
        kw["updated"] = iso(now)
        if "attempts" in kw:
            kw["attempts"] = json.dumps(kw["attempts"])
        self.x(f"update payouts set {','.join(k + '=?' for k in kw)} where id=?", *kw.values(), pid)

    def _payout_view(self, p, duplicate=False):
        p = dict(p)
        p["dest"], p["attempts"] = json.loads(p["dest"]), json.loads(p["attempts"] or "[]")
        p["duplicate"] = duplicate
        return p

    def get_payout(self, pid):
        p = self.q1("select * from payouts where id=?", pid)
        return self._payout_view(p) if p else None

    # ------------------------------------------------------------------------------------------- cards
    def card_issue(self, d):
        self._need(d, "card_id", "wallet")
        self.x("insert or replace into cards values(?,?,?,?,?,?,?,?)", d["card_id"], d["wallet"], d.get("status", "active"),
               int(d.get("per_txn_limit", 0)), int(d.get("daily_limit", 0)), json.dumps(d.get("blocked_mcc", [])),
               json.dumps(d.get("allowed_countries", [])), d.get("credit_id"))
        return {"card_id": d["card_id"], "status": d.get("status", "active")}

    def card_authorize(self, d, now):
        """Real-time authorization from wallet availability (+ optional credit line). Idempotent per auth_id. Never overspends:
        the Ledger refuses the hold atomically if funds are short, so concurrent authorizations cannot double-spend."""
        t0 = time.perf_counter()
        self._need(d, "auth_id", "card_id", "amount", "ccy")
        prior = self.q1("select * from auths where auth_id=?", d["auth_id"])
        if prior:
            r = json.loads(prior["response"])
            r["duplicate"] = True
            return r
        amount, ccy, aid = int(d["amount"]), d["ccy"], d["auth_id"]

        def finish(approved, reason, code, held_w=0, held_c=0, credit_id=None, wallet=None):
            ts = iso(now)
            exp = iso(now + dt.timedelta(hours=self.auth_ttl_h))
            resp = {"auth_id": aid, "approved": approved, "response_code": RESP[code] if code in RESP else code, "reason": reason,
                    "amount": amount, "ccy": ccy, "latency_ms": round((time.perf_counter() - t0) * 1000, 2), "duplicate": False}
            self.x("insert or ignore into auths values(?,?,?,?,?,?,?,?,?,?,?,?,0,0,?,?,?)", aid, d["card_id"], wallet or "", amount, ccy,
                   d.get("merchant"), d.get("mcc"), d.get("country"), "authorized" if approved else "declined", held_w, held_c,
                   credit_id, ts, exp, json.dumps(resp))
            return resp

        card = self.q1("select * from cards where card_id=?", d["card_id"])
        if not card:
            return finish(False, "unknown card", "INVALID_CARD")
        wid = card["wallet"]
        if card["status"] != "active":
            return finish(False, f"card {card['status']}", "DO_NOT_HONOR", wallet=wid)
        if card["per_txn_limit"] and amount > card["per_txn_limit"]:
            return finish(False, "exceeds per-transaction limit", "EXCEEDS_LIMIT", wallet=wid)
        if d.get("mcc") in json.loads(card["blocked_mcc"]):
            return finish(False, "merchant category blocked", "RESTRICTED", wallet=wid)
        allowed = json.loads(card["allowed_countries"])
        if allowed and d.get("country") and d["country"] not in allowed:
            return finish(False, "country not allowed", "RESTRICTED", wallet=wid)
        if card["daily_limit"]:
            day = iso(now)[:10]
            spent = sum(r["amount"] for r in self.q("select amount from auths where card_id=? and status='authorized' and created like ?",
                                                    d["card_id"], day + "%"))
            if spent + amount > card["daily_limit"]:
                return finish(False, "exceeds daily limit", "EXCEEDS_LIMIT", wallet=wid)
        fac = None
        if card["credit_id"]:
            fac = self.q1("select * from facilities where id=? and status='active'", card["credit_id"])
        try:
            if fac:
                r = self.led.tx(f"auth:{aid}:hold", script=NS_ORDERED_DRAW,
                                vars={"wallet": f"wallet:{wid}", "credit": f"credit:{fac['id']}", "dst": f"holds:{aid}",
                                      "amt": f"{asset(ccy)} {amount}", "limit": f"{asset(ccy)} {fac['limit_minor']}"},
                                metadata={"type": "card_hold", "card": d["card_id"]})
            else:
                r = self.led.tx(f"auth:{aid}:hold", [(f"wallet:{wid}", f"holds:{aid}", amount, asset(ccy))],
                                metadata={"type": "card_hold", "card": d["card_id"]})
        except InsufficientFunds:
            return finish(False, "insufficient funds", "INSUFFICIENT_FUNDS", wallet=wid)
        except LedgerError as e:
            return finish(False, f"ledger error {e.code}", "SYSTEM_ERROR", wallet=wid)
        held_w = held_c = 0
        for p in r["postings"] or []:
            if p["source"].startswith("wallet:"):
                held_w += int(p["amount"])
            elif p["source"].startswith("credit:"):
                held_c += int(p["amount"])
        if r["dup"]:   # hold already existed (crash between ledger commit and local record): recover split from the ledger
            held_w, held_c = amount, 0
        return finish(True, "approved", "APPROVED", held_w, held_c, fac["id"] if fac else None, wallet=wid)

    def _auth(self, aid):
        a = self.q1("select * from auths where auth_id=?", aid)
        if not a:
            raise KeyError(f"unknown auth {aid}")
        return a

    def card_capture(self, d, now):
        """Capture up to the held amount. final=true (default) releases any remainder back to its sources (credit line first)."""
        self._need(d, "auth_id", "amount")
        a = self._auth(d["auth_id"])
        if a["status"] != "authorized":
            raise Declined(f"auth is {a['status']}")
        ccy, amt, aid = a["ccy"], int(d["amount"]), a["auth_id"]
        seq = d.get("seq", 1)
        remaining = a["held_wallet"] + a["held_credit"] - a["captured"] - a["released"]
        if amt > remaining:
            raise Declined("capture exceeds authorized amount")
        post = [(f"holds:{aid}", f"card:clearing:{ccy}", amt, asset(ccy))]
        released = 0
        if d.get("final", True) and remaining - amt > 0:
            rest = remaining - amt
            released = rest
            back_c = min(rest, a["held_credit"])
            if back_c:
                post.append((f"holds:{aid}", f"credit:{a['credit_id']}", back_c, asset(ccy)))
            if rest - back_c:
                post.append((f"holds:{aid}", f"wallet:{a['wallet']}", rest - back_c, asset(ccy)))
        r = self.led.tx(f"auth:{aid}:capture:{seq}", post, metadata={"type": "card_capture"})
        if not r["dup"]:
            self.x("update auths set captured=captured+?, released=released+?, status=? where auth_id=?", amt, released,
                   "captured" if d.get("final", True) else "authorized", aid)
        return {"auth_id": aid, "captured": amt, "released": released, "ledger_tx": r["id"], "duplicate": r["dup"]}

    def card_reverse(self, d, now):
        a = self._auth(d["auth_id"])
        if a["status"] != "authorized":
            return {"auth_id": a["auth_id"], "status": a["status"], "released": 0}
        aid, ccy = a["auth_id"], a["ccy"]
        remaining = a["held_wallet"] + a["held_credit"] - a["captured"] - a["released"]
        post, back_c = [], min(remaining, a["held_credit"])
        if back_c:
            post.append((f"holds:{aid}", f"credit:{a['credit_id']}", back_c, asset(ccy)))
        if remaining - back_c:
            post.append((f"holds:{aid}", f"wallet:{a['wallet']}", remaining - back_c, asset(ccy)))
        if post:
            self.led.tx(f"auth:{aid}:reverse", post, metadata={"type": "card_reverse"})
        self.x("update auths set released=released+?, status='reversed' where auth_id=?", remaining, aid)
        return {"auth_id": aid, "status": "reversed", "released": remaining}

    def card_expire(self, now):
        out = []
        for a in self.q("select auth_id from auths where status='authorized' and expires<=?", iso(now)):
            out.append(self.card_reverse({"auth_id": a["auth_id"]}, now))
        return out

    # ------------------------------------------------------------------------------------------- FX
    def fx_rate(self, sell, buy):
        if sell == buy:
            return 1.0
        def usd_per(c):
            if c == "USD":
                return 1.0
            if f"{c}USD" in self.fx_mid:
                return self.fx_mid[f"{c}USD"]
            raise ValueError(f"no rate for {c}")
        return usd_per(sell) / usd_per(buy)

    def fx_quote(self, d, now):
        self._need(d, "wallet", "sell_ccy", "buy_ccy", "sell_amount")
        sell = int(d["sell_amount"])
        mid = self.fx_rate(d["sell_ccy"], d["buy_ccy"])
        eff = mid * (1 - self.fx_spread_bps / 10_000)
        buy = int(sell * eff)
        qid = "q_" + uuid.uuid4().hex[:12]
        exp = iso(now + dt.timedelta(seconds=self.quote_ttl_s))
        self.x("insert into quotes values(?,?,?,?,?,?,?,?,?)", qid, d["wallet"], d["sell_ccy"], d["buy_ccy"], sell, buy, eff, exp, "open")
        return {"quote_id": qid, "rate": round(eff, 6), "mid": round(mid, 6), "sell": sell, "buy": buy, "expires_at": exp}

    def fx_convert(self, d, now):
        self._need(d, "quote_id")
        qt = self.q1("select * from quotes where id=?", d["quote_id"])
        if not qt:
            raise KeyError("unknown quote")
        if qt["status"] == "used":
            return {"status": "converted", "duplicate": True, "buy": qt["buy"]}
        if iso(now) > qt["expires"]:
            raise Declined("quote expired")
        try:
            r = self.led.tx(f"fx:{qt['id']}", [(f"wallet:{qt['wallet']}", f"fx:pool:{qt['sell_ccy']}", qt["sell"], asset(qt["sell_ccy"])),
                                              (f"fx:pool:{qt['buy_ccy']}", f"wallet:{qt['wallet']}", qt["buy"], asset(qt["buy_ccy"]))],
                            metadata={"type": "fx", "rate": qt["rate"]})
        except InsufficientFunds as e:
            raise Declined("insufficient funds or no FX inventory in the target currency")
        self.x("update quotes set status='used' where id=?", qt["id"])
        return {"status": "converted", "sold": qt["sell"], "bought": qt["buy"], "rate": qt["rate"], "ledger_tx": r["id"], "duplicate": r["dup"]}

    def fx_exposure(self):
        out = {}
        for ccy in ("USD", "EUR", "GBP"):
            out[ccy] = self.led.balance(f"fx:pool:{ccy}", asset(ccy))
        return out

    # ------------------------------------------------------------------------------------------- sweeps & liquidity
    def sweep(self, d):
        self._need(d, "ref", "from", "to", "ccy")
        a, ccy = asset(d["ccy"]), d["ccy"]
        if d.get("mode", "all") == "all":
            bal = self.led.balance(d["from"], a)
            if bal <= 0:
                return {"swept": 0}
            r = self.led.tx(f"sweep:{d['ref']}", script=NS_SWEEP_ALL, vars={"src": d["from"], "dst": d["to"], "ccy": a},
                            metadata={"type": "sweep"})
            return {"swept": sum(int(p["amount"]) for p in (r["postings"] or [])) if not r["dup"] else bal, "ledger_tx": r["id"],
                    "duplicate": r["dup"]}
        target = int(d["target"])      # mode=above: keep `target` in the source, sweep the excess
        excess = self.led.balance(d["from"], a) - target
        if excess <= 0:
            return {"swept": 0}
        r = self.led.tx(f"sweep:{d['ref']}", [(d["from"], d["to"], excess, a)], metadata={"type": "sweep"})
        return {"swept": excess, "ledger_tx": r["id"], "duplicate": r["dup"]}

    def liquidity(self, targets=None):
        """Per bank: bank-reported cash, committed in-flight, and suggested rebalancing vs targets (min cash to keep prefunded)."""
        targets, view = targets or {}, {}
        for name, b in self.banks.items():
            for ccy in sorted(b.ccys - {"*"}):
                try:
                    cash = b.balance(ccy)
                except AdapterError:
                    cash = None
                view.setdefault(name, {})[ccy] = {"bank_available": cash,
                                                  "inflight_committed": self.led.balance(f"inflight:{name}:{ccy}", asset(ccy)),
                                                  "target": targets.get(f"{name}:{ccy}")}
        moves = []
        for ccy in ("USD",):
            low = [(n, v[ccy]) for n, v in view.items() if ccy in v and v[ccy]["target"] and v[ccy]["bank_available"] is not None
                   and v[ccy]["bank_available"] < v[ccy]["target"]]
            high = [(n, v[ccy]) for n, v in view.items() if ccy in v and v[ccy]["target"] and v[ccy]["bank_available"] is not None
                    and v[ccy]["bank_available"] > v[ccy]["target"]]
            for ln, lv in low:
                for hn, hv in high:
                    amt = min(lv["target"] - lv["bank_available"], hv["bank_available"] - hv["target"])
                    if amt > 0:
                        moves.append({"from_bank": hn, "to_bank": ln, "ccy": ccy, "amount": amt,
                                      "suggested_rail": "FEDNOW" if amt <= self.rails["FEDNOW"]["max_minor"] else "FEDWIRE"})
        return {"banks": view, "suggested_moves": moves}

    # ------------------------------------------------------------------------------------------- credit
    def credit_open(self, d, now):
        self._need(d, "facility_id", "borrower", "ccy", "limit")
        self.x("insert or ignore into facilities values(?,?,?,?,?,?,?)", d["facility_id"], d["borrower"], d["ccy"], int(d["limit"]),
               int(d.get("apr_bps", 0)), "active", iso(now)[:10])
        return {"facility_id": d["facility_id"], "limit": int(d["limit"]), "status": "active"}

    def _fac(self, fid):
        f = self.q1("select * from facilities where id=?", fid)
        if not f:
            raise KeyError(f"unknown facility {fid}")
        return f

    def credit_status(self, fid):
        f = self._fac(fid)
        bal = self.led.balance(f"credit:{fid}", asset(f["ccy"]))
        return {"facility_id": fid, "limit": f["limit_minor"], "outstanding": max(0, -bal), "available": max(0, f["limit_minor"] + bal),
                "apr_bps": f["apr_bps"], "status": f["status"]}

    def credit_draw(self, d):
        self._need(d, "facility_id", "ref", "amount")
        f = self._fac(d["facility_id"])
        a = asset(f["ccy"])
        try:
            r = self.led.tx(f"credit:{f['id']}:draw:{d['ref']}", script=NS_CREDIT_DRAW,
                            vars={"credit": f"credit:{f['id']}", "wallet": f"wallet:{f['borrower']}", "amt": f"{a} {int(d['amount'])}",
                                  "limit": f"{a} {f['limit_minor']}"}, metadata={"type": "credit_draw"})
        except InsufficientFunds:
            raise Declined("exceeds available credit")
        return dict(self.credit_status(f["id"]), ledger_tx=r["id"], duplicate=r["dup"])

    def credit_repay(self, d):
        self._need(d, "facility_id", "ref", "amount")
        f = self._fac(d["facility_id"])
        out = max(0, -self.led.balance(f"credit:{f['id']}", asset(f["ccy"])))
        amt = min(int(d["amount"]), out)
        if amt <= 0:
            return dict(self.credit_status(f["id"]), repaid=0)
        try:
            r = self.led.tx(f"credit:{f['id']}:repay:{d['ref']}", [(f"wallet:{f['borrower']}", f"credit:{f['id']}", amt, asset(f["ccy"]))],
                            metadata={"type": "credit_repay"})
        except InsufficientFunds:
            raise Declined("insufficient funds in borrower wallet")
        return dict(self.credit_status(f["id"]), repaid=amt, ledger_tx=r["id"], duplicate=r["dup"])

    def credit_accrue(self, d, now):
        """Daily simple interest on outstanding principal, capitalised into the line. Idempotent per facility per date."""
        f = self._fac(d["facility_id"])
        day = d.get("date") or iso(now)[:10]
        a = asset(f["ccy"])
        out = max(0, -self.led.balance(f"credit:{f['id']}", a))
        interest = out * f["apr_bps"] // 10_000 // 365
        if interest <= 0:
            return dict(self.credit_status(f["id"]), interest=0)
        r = self.led.tx(f"credit:{f['id']}:interest:{day}", script=NS_INTEREST,
                        vars={"credit": f"credit:{f['id']}", "income": f"income:interest:{f['ccy']}", "amt": f"{a} {interest}"},
                        metadata={"type": "interest"})
        return dict(self.credit_status(f["id"]), interest=interest, duplicate=r["dup"])

    # ------------------------------------------------------------------------------------------- guarantees
    def guarantee_issue(self, d, now):
        self._need(d, "guarantee_id", "applicant", "beneficiary", "ccy", "amount")
        gid, a, amt = d["guarantee_id"], asset(d["ccy"]), int(d["amount"])
        collateral = int(amt * float(d.get("collateral_pct", 100)) / 100)
        if collateral:
            try:
                self.led.tx(f"guarantee:{gid}:collateral", [(f"wallet:{d['applicant']}", f"collateral:{gid}", collateral, a)],
                            metadata={"type": "guarantee_collateral"})
            except InsufficientFunds:
                raise Declined("insufficient funds for collateral")
        self.led.tx(f"guarantee:{gid}:memo", script=NS_MEMO,
                    vars={"issued": f"memo:guarantees:issued:{d['ccy']}", "memo": f"memo:guarantee:{gid}", "amt": f"{a} {amt}"},
                    metadata={"type": "guarantee_memo"})
        self.x("insert or ignore into guarantees values(?,?,?,?,?,?,0,?,?)", gid, d["applicant"], d["beneficiary"], d["ccy"], amt,
               collateral, "active", d.get("expiry"))
        return {"guarantee_id": gid, "amount": amt, "collateral_held": collateral, "status": "active"}

    def guarantee_claim(self, d):
        self._need(d, "guarantee_id", "ref", "amount")
        g = self.q1("select * from guarantees where id=?", d["guarantee_id"])
        if not g or g["status"] != "active":
            raise Declined("guarantee not active")
        amt = int(d["amount"])
        if g["claimed"] + amt > g["amount"]:
            raise Declined("claim exceeds guaranteed amount")
        a, gid = asset(g["ccy"]), g["id"]
        r = self.led.tx(f"guarantee:{gid}:claim:{d['ref']}", script=NS_GUARANTEE_CLAIM,
                        vars={"collateral": f"collateral:{gid}", "applicant": f"wallet:{g['applicant']}",
                              "receivable": f"receivable:guarantee:{gid}", "beneficiary": f"wallet:{g['beneficiary']}", "amt": f"{a} {amt}"},
                        metadata={"type": "guarantee_claim"})
        if not r["dup"]:
            self.x("update guarantees set claimed=claimed+? where id=?", amt, gid)
        return {"guarantee_id": gid, "paid": amt, "receivable_from_applicant": max(0, -self.led.balance(f"receivable:guarantee:{gid}", a)),
                "duplicate": r["dup"]}

    def guarantee_release(self, d):
        g = self.q1("select * from guarantees where id=?", d["guarantee_id"])
        if not g or g["status"] != "active":
            raise Declined("guarantee not active")
        a, gid = asset(g["ccy"]), g["id"]
        left = self.led.balance(f"collateral:{gid}", a)
        if left > 0:
            self.led.tx(f"guarantee:{gid}:release", [(f"collateral:{gid}", f"wallet:{g['applicant']}", left, a)])
        self.x("update guarantees set status='released' where id=?", gid)
        return {"guarantee_id": gid, "collateral_returned": left, "status": "released"}

    # ------------------------------------------------------------------------------------------- inbound & reconciliation
    def apply_inbound(self, bank, e):
        """Post a bank credit to the Ledger (idempotent on bank_ref). Attributable via narrative 'WALLET:<id>', else suspense."""
        a = asset(e["ccy"])
        wid = e["narrative"].split("WALLET:")[1].split()[0] if "WALLET:" in e["narrative"] else None
        dest = f"wallet:{wid}" if wid else f"unapplied:{bank}:{e['ccy']}"
        r = self.led.tx(f"inbound:{bank}:{e['bank_ref']}", [("world", dest, int(e["amount"]), a)],
                        metadata={"type": "inbound", "bank_ref": e["bank_ref"], "rail": e.get("rail", "")})
        return {"bank_ref": e["bank_ref"], "to": dest, "duplicate": r["dup"]}

    def reconcile(self, bank_name, now, since=None):
        """Match the bank statement to the Ledger. Credits are applied; debits must match settled payouts by bank_ref and amount.
        Anything unexplained is reported, never silently absorbed. Safe to re-run."""
        bank = self.banks[bank_name]
        rep = {"bank": bank_name, "matched": [], "credits_applied": [], "credits_unapplied": [], "unmatched_bank_debits": [],
               "amount_mismatch": [], "ledger_settled_not_on_statement": []}
        seen = set()
        for e in bank.statement(since):
            seen.add(e["bank_ref"])
            if e["direction"] == "credit":
                r = self.apply_inbound(bank_name, e)
                (rep["credits_unapplied"] if r["to"].startswith("unapplied:") else rep["credits_applied"]).append(
                    {"bank_ref": e["bank_ref"], "to": r["to"], "already_posted": r["duplicate"]})
                continue
            pid = e["narrative"].split("PAY:")[1].split()[0] if "PAY:" in e["narrative"] else None
            p = self.q1("select * from payouts where id=?", pid) if pid else None
            if not p or p["bank_ref"] != e["bank_ref"]:
                rep["unmatched_bank_debits"].append(e)
            elif p["amount"] != e["amount"] or p["ccy"] != e["ccy"]:
                rep["amount_mismatch"].append({"bank_ref": e["bank_ref"], "bank": e["amount"], "ledger": p["amount"]})
            else:
                rep["matched"].append(e["bank_ref"])
        for p in self.q("select * from payouts where bank=? and status='settled'", bank_name):
            if p["bank_ref"] not in seen and (since is None):
                rep["ledger_settled_not_on_statement"].append({"payout": p["id"], "bank_ref": p["bank_ref"]})
        for kind in ("unmatched_bank_debits", "amount_mismatch", "ledger_settled_not_on_statement", "credits_unapplied"):
            for it in rep[kind]:
                ref = it.get("bank_ref") or it.get("payout")
                self.x("insert or replace into recon values(?,?,?,?,?)", bank_name, ref, kind, "open", json.dumps(it))
        rep["clean"] = not (rep["unmatched_bank_debits"] or rep["amount_mismatch"] or rep["ledger_settled_not_on_statement"])
        rep["ledger_inflight"] = {c: self.led.balance(f"inflight:{bank_name}:{c}", asset(c)) for c in ("USD", "EUR", "GBP")
                                  if c in bank.ccys}
        return rep

    # ------------------------------------------------------------------------------------------- Rafiki (Interledger) intake
    def rafiki_event(self, ev):
        """Post a Rafiki webhook into the same wallets. Idempotent on the webhook event id (so Rafiki's retries are harmless)."""
        t, dta = ev["type"], ev["data"]
        wa, pid = dta.get("walletAddressId"), dta["id"]
        def amt(k):
            return int(dta[k]["value"]), f"{dta[k]['assetCode']}/{dta[k]['assetScale']}"
        if t == "outgoing_payment.created":
            v, a = amt("debitAmount"); post = [(f"wallet:{wa}", f"rafiki:inflight:{pid}", v, a)]
        elif t == "outgoing_payment.completed":
            v, a = amt("sentAmount"); post = [(f"rafiki:inflight:{pid}", "rafiki:settlement", v, a)]
        elif t == "outgoing_payment.failed":
            v, a = amt("debitAmount"); post = [(f"rafiki:inflight:{pid}", f"wallet:{wa}", v, a)]
        elif t == "incoming_payment.completed":
            v, a = amt("receivedAmount"); post = [("rafiki:settlement", f"wallet:{wa}", v, a)]
        else:
            return {"ignored": t}
        try:
            r = self.led.tx(f"rafiki:{ev['id']}", post, metadata={"type": t})
        except InsufficientFunds:
            raise Declined("insufficient funds for Rafiki outgoing payment")
        return {"posted": t, "ledger_tx": r["id"], "duplicate": r["dup"]}
