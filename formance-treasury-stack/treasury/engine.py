# SPDX-License-Identifier: Apache-2.0
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
import hashlib
import json
import os
import re
import threading
import time
import uuid

import rails as railmod
from banks import AdapterError, Rejected
from ledger import IdempotencyConflict, InsufficientFunds, LedgerError
from store import Store

# Wallet account naming. Default `wallet:<id>`. Point the engine at an existing ledger layout with
#   WALLET_ACCOUNT_PREFIX=liquidity:wallet:                      -> wallet "corpus-reflection" is account liquidity:wallet:corpus-reflection
#   WALLET_ALIASES_JSON={"corpus-6b":"treasury:available:corpus-6b"}   -> wallet id mapped to an arbitrary account (cards can hold against it)
# Card pre-authorization, payouts, transfers, FX, credit and guarantees all resolve wallets through W().
WALLET_PREFIX = os.environ.get("WALLET_ACCOUNT_PREFIX", "wallet:")
WALLET_ALIASES = json.loads(os.environ.get("WALLET_ALIASES_JSON", "{}") or "{}")


def W(wid):
    return WALLET_ALIASES.get(wid) or WALLET_PREFIX + wid


def is_wallet(addr):
    return addr.startswith(WALLET_PREFIX) or addr in WALLET_ALIASES.values()


RESP = {"APPROVED": "00", "DO_NOT_HONOR": "05", "INSUFFICIENT_FUNDS": "51", "EXCEEDS_LIMIT": "61", "RESTRICTED": "62",
        "INVALID_CARD": "14", "SYSTEM_ERROR": "96"}

ID_RE = re.compile(r"^[A-Za-z0-9._:@-]{1,96}$")
CCY_RE = re.compile(r"^[A-Z]{3}$")
MAX_MINOR = 10**13          # hard ceiling on any single amount (minor units)


class Declined(Exception):
    def __init__(self, reason, code=None):
        super().__init__(reason)
        self.reason, self.code = reason, code


class ValidationError(ValueError):
    pass


class NotFound(Exception):
    """Unknown payout / authorization / quote / facility id. Maps to HTTP 404 (a bare KeyError is a bug and must surface as 500)."""


class SimulatedCrash(BaseException):
    """Fault injection (tests only): models the process dying at a precise point. BaseException so no handler swallows it."""


def asset(ccy):
    return f"{ccy}/2"


def iso(d):
    return d.isoformat() if isinstance(d, dt.datetime) else d


def _fp(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()[:32]


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


NS_CLEARING_CREDIT = """vars {
  account $clearing
  account $wallet
  monetary $amt
}
send $amt (
  source = $clearing allowing unbounded overdraft
  destination = $wallet
)"""


TERMINAL = ("settled", "failed")
IN_FLIGHT = ("new", "reserved", "routing")


class Treasury:
    def __init__(self, ledger, banks, store=None, fx_rates=None, fx_spread_bps=40, auth_ttl_h=168, quote_ttl_s=30, lease_s=30):
        self.led, self.banks = ledger, banks
        self.db = store or Store(":memory:")
        for bk in self.banks.values():
            if hasattr(bk, "bind"):
                bk.bind(self.db)          # operator-assisted banks keep their queue/statement state in the same Store
        self.rails, self.hol = railmod.load_rails(), railmod.holidays()
        self.fx_mid = fx_rates or {"EURUSD": 1.0850, "GBPUSD": 1.2700}
        self.fx_spread_bps, self.auth_ttl_h, self.quote_ttl_s, self.lease_s = fx_spread_bps, auth_ttl_h, quote_ttl_s, lease_s
        self.crash_at = None            # fault-injection point name (tests only)
        self.ctx = threading.local()    # per-request actor / request id for audit

    # ------------------------------------------------------------------------------------------- plumbing
    def q(self, sql, *a): return self.db.q(sql, *a)
    def q1(self, sql, *a): return self.db.q1(sql, *a)
    def x(self, sql, *a): return self.db.x(sql, *a)

    def _crash(self, point):
        if self.crash_at == point:
            self.crash_at = None
            raise SimulatedCrash(point)

    def audit(self, action, ref, detail=None):
        try:
            self.x("insert into audit values(?,?,?,?,?,?) on conflict do nothing", uuid.uuid4().hex,
                   dt.datetime.now(dt.timezone.utc).isoformat(), getattr(self.ctx, "actor", "system"), action, ref,
                   json.dumps(detail or {}, default=str))
        except Exception:      # auditing must never break a money movement that already happened
            pass

    def ready(self):
        return {"ledger": self.led.healthy(), "db": self.db.ping()}

    # ------------------------------------------------------------------------------------------- validation
    @staticmethod
    def _need(d, *keys):
        if not isinstance(d, dict):
            raise ValidationError("body must be a JSON object")
        for k in keys:
            if d.get(k) in (None, ""):
                raise ValidationError(f"missing field: {k}")

    @staticmethod
    def _id(d, k):
        v = d.get(k)
        if not isinstance(v, str) or not ID_RE.match(v):
            raise ValidationError(f"invalid {k}: 1-96 chars of [A-Za-z0-9._:@-]")
        return v

    @staticmethod
    def _ccy(d, k="ccy"):
        v = d.get(k)
        if not isinstance(v, str) or not CCY_RE.match(v):
            raise ValidationError(f"invalid {k}: ISO 4217 code like USD")
        return v

    @staticmethod
    def _amt(d, k="amount"):
        v = d.get(k)
        if isinstance(v, bool) or not isinstance(v, int) or not (0 < v <= MAX_MINOR):
            raise ValidationError(f"invalid {k}: positive integer minor units (max {MAX_MINOR})")
        return v

    # ------------------------------------------------------------------------------------------- wallets & transfers
    def _auth_totals(self, aid):
        r = self.q1("select coalesce(sum(amount),0) c, coalesce(sum(released),0) r from auth_events where auth_id=?", aid)
        return int(r["c"]), int(r["r"])

    def wallet(self, wid, ccy="USD"):
        a = asset(ccy)
        bal = self.led.balance(W(wid), a)
        held = 0
        for r in self.q("select a.held_wallet hw, a.held_credit hc, coalesce(sum(e.amount),0) c, coalesce(sum(e.released),0) r "
                        "from auths a left join auth_events e on e.auth_id=a.auth_id "
                        "where a.wallet=? and a.ccy=? and a.status='authorized' group by a.auth_id, a.held_wallet, a.held_credit", wid, ccy):
            held += int(r["hw"]) + int(r["hc"]) - int(r["c"]) - int(r["r"])
        fac = self.q1("select * from facilities where borrower=? and ccy=? and status='active'", wid, ccy)
        credit_avail = 0
        if fac:
            credit_avail = max(0, int(fac["limit_minor"]) + self.led.balance(f"credit:{fac['id']}", a))
        return {"wallet": wid, "ccy": ccy, "balance": bal, "card_holds": held, "credit_available": credit_avail,
                "spendable": bal + credit_avail}

    def transfer(self, d):
        ref, src, dst, amt, ccy = self._id(d, "ref"), self._id(d, "from"), self._id(d, "to"), self._amt(d), self._ccy(d)
        try:
            r = self.led.tx(f"transfer:{ref}", [(W(src), W(dst), amt, asset(ccy))], metadata={"type": "transfer"})
        except InsufficientFunds:
            raise Declined("insufficient funds", "51")
        self.audit("transfer", ref, {"from": src, "to": dst, "amount": amt, "ccy": ccy})
        return {"status": "completed", "ledger_tx": r["id"], "duplicate": r["dup"]}

    # ------------------------------------------------------------------------------------------- routing
    def route_plan(self, amount, ccy, dest, urgency, now):
        """Ordered candidates [{bank, rail, instant, eta, fee, liquidity}] honouring rail windows, limits, bank support, liquidity."""
        intl = dest.get("type") == "international" or ccy != "USD"
        order = ["SWIFT"] if intl else (["FEDNOW", "RTP", "FEDWIRE", "ACH"] if urgency == "instant" else ["ACH", "FEDWIRE", "FEDNOW", "RTP"])
        liq_cache, out = {}, []
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
                if (bank.name, ccy) not in liq_cache:
                    try:
                        liq_cache[(bank.name, ccy)] = bank.balance(ccy)
                    except AdapterError:
                        liq_cache[(bank.name, ccy)] = None
                liq = liq_cache[(bank.name, ccy)]
                if liq is None or liq < amount:
                    continue
                out.append({"bank": bank.name, "rail": rail, "instant": av["instant"], "eta": iso(av["eta"]), "fee": spec["fee_minor"],
                            "liquidity": liq, "rank": rank})
        out.sort(key=lambda c: (c["rank"], -c["liquidity"]))     # rail preference first, then the most liquid bank
        return out

    # ------------------------------------------------------------------------------------------- payouts (write-ahead state machine)
    # States: new -> reserved -> routing -> settled | accepted(in flight, polled) | review(outcome unknown) | failed.
    # Every ledger step has a deterministic reference and every bank call is idempotent on the payout id, so ANY step can be
    # replayed after a crash. A lease prevents two workers driving the same payout (client retry vs background recovery).
    def payout(self, d, now):
        pid, wid, amount, ccy = self._id(d, "id"), self._id(d, "wallet"), self._amt(d), self._ccy(d)
        dest = d.get("dest")
        if not isinstance(dest, dict) or dest.get("type") not in ("us_account", "international"):
            raise ValidationError("dest.type must be 'us_account' or 'international'")
        urgency = d.get("urgency", "instant")
        if urgency not in ("instant", "standard"):
            raise ValidationError("urgency must be 'instant' or 'standard'")
        fp = _fp(wid, amount, ccy, dest, urgency)
        ts = iso(now)
        created = self.x("insert into payouts(id, req_fp, wallet, amount, ccy, dest, urgency, status, attempts, plan, created, updated) "
                         "values(?,?,?,?,?,?,?,?,?,?,?,?) on conflict do nothing",
                         pid, fp, wid, amount, ccy, json.dumps(dest), urgency, "new", "[]", None, ts, ts)
        row = self.q1("select * from payouts where id=?", pid)
        if row["req_fp"] != fp:
            raise IdempotencyConflict(f"payout id {pid!r} was already used for a different request")
        self.audit("payout.request", pid, {"wallet": wid, "amount": amount, "ccy": ccy, "new": bool(created)})
        self._crash("after_insert")
        return self._drive(pid, now, duplicate=not created)

    def _claim(self, pid, force=False):
        now = time.time()
        if force:
            self.x("update payouts set lease_until=? where id=?", now + self.lease_s, pid)
            return True
        return self.x("update payouts set lease_until=? where id=? and (lease_until is null or lease_until < ?)",
                      now + self.lease_s, pid, now) == 1

    def _unlease(self, pid):
        self.x("update payouts set lease_until=null where id=?", pid)

    def _drive(self, pid, now, duplicate=False, force=False):
        row = self.q1("select * from payouts where id=?", pid)
        if row["status"] in TERMINAL + ("review", "accepted"):
            return self._payout_view(row, duplicate)
        if not self._claim(pid, force):
            v = self._payout_view(row, duplicate)
            v["in_progress"] = True       # another worker is driving it; poll GET /payouts/{id}
            return v
        try:
            self._advance(pid, now)
        except SimulatedCrash:
            raise                         # a real crash cannot release the lease; it expires on its own
        except BaseException:
            self._unlease(pid)
            raise
        self._unlease(pid)
        return self._payout_view(self.q1("select * from payouts where id=?", pid), duplicate)

    def _set(self, pid, now, **kw):
        kw["updated"] = iso(now)
        for k in ("attempts", "plan"):
            if k in kw and not isinstance(kw[k], str) and kw[k] is not None:
                kw[k] = json.dumps(kw[k])
        self.x(f"update payouts set {','.join(k + '=?' for k in kw)} where id=?", *kw.values(), pid)

    def _advance(self, pid, now):
        p = self.q1("select * from payouts where id=?", pid)
        amount, ccy, wid, a = int(p["amount"]), p["ccy"], p["wallet"], asset(p["ccy"])
        if p["status"] == "new":
            try:
                self.led.tx(f"payout:{pid}:reserve", [(W(wid), f"payout:{pid}", amount, a)], metadata={"type": "payout"})
            except InsufficientFunds:
                self._set(pid, now, status="failed", reason="insufficient funds")
                raise Declined("insufficient funds", "51")
            self._set(pid, now, status="reserved")
            self._crash("after_reserve")
            p = self.q1("select * from payouts where id=?", pid)
        dest = json.loads(p["dest"])
        plan = json.loads(p["plan"]) if p["plan"] else None
        if plan is None:
            plan = self.route_plan(amount, ccy, dest, p["urgency"], now)
            self._set(pid, now, plan=plan)
        attempts = json.loads(p["attempts"] or "[]")
        if not plan:
            return self._fail(pid, "no_route", attempts, now)
        payment = {"id": pid, "amount": amount, "ccy": ccy, "dest": dest}
        resumed = bool(attempts) and attempts[-1]["result"] == "pending"
        k = len(attempts) - 1 if resumed else len(attempts)
        while k < len(plan):
            c = plan[k]
            bank, fl = self.banks[c["bank"]], f"inflight:{c['bank']}:{ccy}"
            if k == len(attempts):           # new attempt: commit liquidity, then write the intent BEFORE calling the bank
                self.led.tx(f"payout:{pid}:route:{k}", [(f"payout:{pid}", fl, amount, a)],
                            metadata={"type": "payout_route", "bank": c["bank"], "rail": c["rail"]})
                attempts.append({"bank": c["bank"], "rail": c["rail"], "result": "pending"})
                self._set(pid, now, status="routing", bank=c["bank"], rail=c["rail"], attempts=attempts)
                self._crash("after_route")
            res, was_resumed = None, resumed
            if resumed:                      # a previous incarnation may already have sent it: ask the bank first
                try:
                    found = bank.lookup(pid)
                except AdapterError:
                    return                   # bank unreachable: stay 'routing'; the recovery loop retries (never fail over blindly)
                if found:
                    res = {"bank_ref": found["bank_ref"], "status": found["status"], "eta": now}
                resumed = False
            if res is None:
                try:
                    res = bank.submit(c["rail"], payment, now)      # idempotent on payment id (adapter contract)
                except Rejected as e:
                    self.led.tx(f"payout:{pid}:unroute:{k}", [(fl, f"payout:{pid}", amount, a)], metadata={"type": "payout_unroute"})
                    attempts[k] = {**attempts[k], "result": "rejected", "reason": e.reason}
                    self._set(pid, now, attempts=attempts)
                    k += 1
                    continue
                except AdapterError as e:
                    if e.sent is False and was_resumed:
                        return               # a previous run may have sent it: never fail over; recovery retries this bank
                    if e.sent is False:
                        self.led.tx(f"payout:{pid}:unroute:{k}", [(fl, f"payout:{pid}", amount, a)], metadata={"type": "payout_unroute"})
                        attempts[k] = {**attempts[k], "result": "unreachable", "reason": str(e)}
                        self._set(pid, now, attempts=attempts)
                        k += 1
                        continue
                    attempts[k] = {**attempts[k], "result": "unknown", "reason": str(e)}
                    self._set(pid, now, status="review", attempts=attempts, reason="bank outcome unknown; resolve by lookup before any retry")
                    return
            self._crash("after_submit")
            attempts[k] = {**attempts[k], "result": res["status"], "bank_ref": res["bank_ref"]}
            self._set(pid, now, status="accepted" if res["status"] == "accepted" else res["status"], bank=c["bank"], rail=c["rail"],
                      bank_ref=res["bank_ref"], eta=iso(res["eta"]), attempts=attempts)
            if res["status"] == "settled":
                self._settle_payout(pid, now)
            elif res["status"] == "failed":
                self._bank_failed(pid, now)
            return
        return self._fail(pid, "all_routes_failed", attempts, now)

    def _fail(self, pid, reason, attempts, now):
        p = self.q1("select * from payouts where id=?", pid)
        self.led.tx(f"payout:{pid}:release", [(f"payout:{pid}", W(p['wallet']), int(p["amount"]), asset(p["ccy"]))],
                    metadata={"type": "payout_release"})
        self._set(pid, now, status="failed", attempts=attempts, reason=reason)
        self.audit("payout.failed", pid, {"reason": reason})

    def _settle_payout(self, pid, now):
        p = self.q1("select * from payouts where id=?", pid)
        self.led.tx(f"payout:{pid}:settle", [(f"inflight:{p['bank']}:{p['ccy']}", "world", int(p["amount"]), asset(p["ccy"]))],
                    metadata={"type": "payout_settle", "bank": p["bank"], "bank_ref": p["bank_ref"], "rail": p["rail"]})
        self._crash("after_settle_tx")
        self._set(pid, now, status="settled")
        self.audit("payout.settled", pid, {"bank": p["bank"], "bank_ref": p["bank_ref"], "rail": p["rail"]})

    def _bank_failed(self, pid, now):
        p = self.q1("select * from payouts where id=?", pid)
        a = asset(p["ccy"])
        self.led.tx(f"payout:{pid}:bankfail", [(f"inflight:{p['bank']}:{p['ccy']}", W(p['wallet']), int(p["amount"]), a)],
                    metadata={"type": "payout_bankfail"})
        self._set(pid, now, status="failed", reason="bank reported failure")

    def poll_payouts(self, now):
        """Advance deferred payouts (ACH/Fedwire/SWIFT): settle what the bank reports settled, refund what it failed."""
        done = []
        for r in self.q("select id, bank, bank_ref from payouts where status='accepted'"):
            try:
                st = self.banks[r["bank"]].status(r["bank_ref"], now)
            except AdapterError:
                continue
            if st == "settled":
                self._settle_payout(r["id"], now)
            elif st == "failed":
                self._bank_failed(r["id"], now)
            done.append({"id": r["id"], "status": self.q1("select status from payouts where id=?", r["id"])["status"]})
        return done

    def resolve_payout(self, pid, now):
        """Resolve an unknown-outcome payout by asking the bank. Found -> adopt its state. Definitively absent -> release funds."""
        p = self.q1("select * from payouts where id=?", pid)
        if not p:
            raise NotFound("unknown payout")
        if p["status"] != "review":
            return self._payout_view(p)
        bank = self.banks[p["bank"]]
        try:
            found = bank.lookup(pid)
        except AdapterError:
            return self._payout_view(p)            # still unknowable; stays in review
        attempts = json.loads(p["attempts"] or "[]")
        if found:
            attempts[-1] = {**attempts[-1], "result": found["status"], "bank_ref": found["bank_ref"]}
            self._set(pid, now, status="accepted" if found["status"] == "accepted" else found["status"], bank_ref=found["bank_ref"],
                      attempts=attempts, reason=None)
            if found["status"] == "settled":
                self._settle_payout(pid, now)
            elif found["status"] == "failed":
                self._bank_failed(pid, now)
        else:
            k = len(attempts) - 1
            a = asset(p["ccy"])
            self.led.tx(f"payout:{pid}:unroute:{k}", [(f"inflight:{p['bank']}:{p['ccy']}", f"payout:{pid}", int(p["amount"]), a)],
                        metadata={"type": "payout_unroute"})
            attempts[k] = {**attempts[k], "result": "not_found_at_bank"}
            self._fail(pid, "bank has no record of the instruction; funds released", attempts, now)
        self.audit("payout.resolved", pid, {"found": bool(found)})
        return self._payout_view(self.q1("select * from payouts where id=?", pid))

    def recover(self, now, stale_s=60, force=False):
        """Background/ops sweep: resume payouts stuck mid-flight, resolve unknown outcomes, settle deferred ones."""
        cutoff = iso(now - dt.timedelta(seconds=stale_s))
        out = {"resumed": [], "resolved": [], "polled": []}
        for r in self.q("select id from payouts where status in ('new','reserved','routing') and updated <= ?", cutoff):
            try:
                v = self._drive(r["id"], now, force=force)
                out["resumed"].append({"id": r["id"], "status": v["status"]})
            except Declined:
                out["resumed"].append({"id": r["id"], "status": "failed"})
        for r in self.q("select id from payouts where status='review'"):
            out["resolved"].append({"id": r["id"], "status": self.resolve_payout(r["id"], now)["status"]})
        out["polled"] = self.poll_payouts(now)
        return out

    def _payout_view(self, p, duplicate=False):
        p = dict(p)
        for k in ("req_fp", "lease_until", "plan"):
            p.pop(k, None)
        p["dest"], p["attempts"] = json.loads(p["dest"]), json.loads(p["attempts"] or "[]")
        p["duplicate"] = duplicate
        return p

    def get_payout(self, pid):
        p = self.q1("select * from payouts where id=?", pid)
        return self._payout_view(p) if p else None

    # ------------------------------------------------------------------------------------------- cards
    def card_issue(self, d):
        cid, wid = self._id(d, "card_id"), self._id(d, "wallet")
        self.x("insert into cards values(?,?,?,?,?,?,?,?) on conflict(card_id) do update set wallet=excluded.wallet, status=excluded.status, "
               "per_txn_limit=excluded.per_txn_limit, daily_limit=excluded.daily_limit, blocked_mcc=excluded.blocked_mcc, "
               "allowed_countries=excluded.allowed_countries, credit_id=excluded.credit_id",
               cid, wid, d.get("status", "active"), int(d.get("per_txn_limit", 0)), int(d.get("daily_limit", 0)),
               json.dumps(d.get("blocked_mcc", [])), json.dumps(d.get("allowed_countries", [])), d.get("credit_id"))
        self.audit("card.issue", cid, {"wallet": wid, "status": d.get("status", "active")})
        return {"card_id": cid, "status": d.get("status", "active")}

    def card_authorize(self, d, now):
        """Real-time authorization from wallet availability (+ optional credit line). Idempotent per auth_id and bound to the request.
        Never overspends: the Ledger refuses the hold atomically if funds are short, so concurrent authorizations cannot double-spend."""
        t0 = time.perf_counter()
        aid, cid, amount, ccy = self._id(d, "auth_id"), self._id(d, "card_id"), self._amt(d), self._ccy(d)
        fp = _fp(cid, amount, ccy, d.get("merchant"), d.get("mcc"), d.get("country"))
        prior = self.q1("select req_fp, response from auths where auth_id=?", aid)
        if prior:
            if prior["req_fp"] != fp:
                raise IdempotencyConflict(f"auth_id {aid!r} was already used for a different request")
            r = json.loads(prior["response"])
            r["duplicate"] = True
            return r
        hold_ref = f"auth:{aid}:hold"

        def finish(approved, reason, code, held_w=0, held_c=0, credit_id=None, wallet="", persist=True):
            exp = iso(now + dt.timedelta(hours=self.auth_ttl_h))
            resp = {"auth_id": aid, "approved": approved, "response_code": RESP.get(code, code), "reason": reason, "amount": amount,
                    "ccy": ccy, "latency_ms": round((time.perf_counter() - t0) * 1000, 2), "duplicate": False}
            if persist:
                self.x("insert into auths(auth_id, req_fp, card_id, wallet, amount, ccy, merchant, mcc, country, status, held_wallet, "
                       "held_credit, credit_id, captured, released, created, expires, response) "
                       "values(?,?,?,?,?,?,?,?,?,?,?,?,?,0,0,?,?,?) on conflict do nothing",
                       aid, fp, cid, wallet, amount, ccy, d.get("merchant"), d.get("mcc"), d.get("country"),
                       "authorized" if approved else "declined", held_w, held_c, credit_id, iso(now), exp, json.dumps(resp))
            return resp

        def adopt_existing_hold(wid, fac):
            """Crash recovery: a hold from a previous attempt exists in the Ledger but we have no record. The Ledger is the truth."""
            t = self.led.tx_by_reference(hold_ref)
            if not t:
                return None
            hw = sum(int(p["amount"]) for p in t["postings"] if is_wallet(p["source"]))
            hc = sum(int(p["amount"]) for p in t["postings"] if p["source"].startswith("credit:"))
            return finish(True, "approved", "APPROVED", hw, hc, fac["id"] if fac and hc else None, wid)

        card = self.q1("select * from cards where card_id=?", cid)
        if not card:
            return finish(False, "unknown card", "INVALID_CARD")
        wid = card["wallet"]
        fac = self.q1("select * from facilities where id=? and status='active'", card["credit_id"]) if card["credit_id"] else None
        decline = None
        if card["status"] != "active":
            decline = (f"card {card['status']}", "DO_NOT_HONOR")
        elif card["per_txn_limit"] and amount > card["per_txn_limit"]:
            decline = ("exceeds per-transaction limit", "EXCEEDS_LIMIT")
        elif d.get("mcc") in json.loads(card["blocked_mcc"]):
            decline = ("merchant category blocked", "RESTRICTED")
        elif json.loads(card["allowed_countries"]) and d.get("country") and d["country"] not in json.loads(card["allowed_countries"]):
            decline = ("country not allowed", "RESTRICTED")
        elif card["daily_limit"]:
            day = iso(now)[:10]
            spent = sum(int(r["amount"]) for r in self.q("select amount from auths where card_id=? and status='authorized' and created like ?",
                                                         cid, day + "%"))
            if spent + amount > card["daily_limit"]:
                decline = ("exceeds daily limit", "EXCEEDS_LIMIT")
        if decline:
            return adopt_existing_hold(wid, fac) or finish(False, decline[0], decline[1], wallet=wid)
        try:
            if fac:
                r = self.led.tx(hold_ref, script=NS_ORDERED_DRAW,
                                vars={"wallet": W(wid), "credit": f"credit:{fac['id']}", "dst": f"holds:{aid}",
                                      "amt": f"{asset(ccy)} {amount}", "limit": f"{asset(ccy)} {fac['limit_minor']}"},
                                metadata={"type": "card_hold", "card": cid})
            else:
                r = self.led.tx(hold_ref, [(W(wid), f"holds:{aid}", amount, asset(ccy))], metadata={"type": "card_hold", "card": cid})
        except InsufficientFunds:
            return finish(False, "insufficient funds", "INSUFFICIENT_FUNDS", wallet=wid)
        except IdempotencyConflict:
            raise
        except LedgerError as e:
            return finish(False, f"ledger unavailable ({e.code})", "SYSTEM_ERROR", wallet=wid, persist=False)   # transient: do not cache
        self._crash("after_hold")
        posted = r["postings"]
        if posted is None:                    # duplicate whose postings the Ledger did not return: read them back (the Ledger is the truth)
            posted = (self.led.tx_by_reference(hold_ref) or {}).get("postings") or []
        hw = sum(int(p["amount"]) for p in posted if is_wallet(p["source"]))
        hc = sum(int(p["amount"]) for p in posted if p["source"].startswith("credit:"))
        if not posted:
            raise LedgerError(500, "HOLD_UNREADABLE", f"hold {hold_ref} exists but its postings could not be read")
        out = finish(True, "approved", "APPROVED", hw, hc, fac["id"] if fac and hc else None, wid)
        self.audit("card.authorize", aid, {"card": cid, "amount": amount, "approved": True})
        return out

    def _auth(self, aid):
        a = self.q1("select * from auths where auth_id=?", aid)
        if not a:
            raise NotFound(f"unknown auth {aid}")
        return a

    def card_capture(self, d, now):
        """Capture up to the held amount. final=true (default) releases the remainder to its sources (credit line first)."""
        aid, amt = self._id(d, "auth_id"), self._amt(d)
        seq = str(d.get("seq", 1))
        a = self._auth(aid)
        prior = self.q1("select * from auth_events where auth_id=? and kind='capture' and seq=?", aid, seq)
        captured, released_tot = self._auth_totals(aid)
        held = int(a["held_wallet"]) + int(a["held_credit"])
        if prior:
            if int(prior["amount"]) != amt:
                raise IdempotencyConflict(f"capture {aid}/{seq} was already applied with a different amount")
            return {"auth_id": aid, "captured": amt, "released": int(prior["released"]), "duplicate": True}
        if a["status"] != "authorized":
            raise Declined(f"auth is {a['status']}")
        remaining = held - captured - released_tot
        if amt > remaining:
            raise Declined("capture exceeds authorized amount")
        ccy, post, released = a["ccy"], [(f"holds:{aid}", f"card:clearing:{a['ccy']}", amt, asset(a["ccy"]))], 0
        if d.get("final", True) and remaining - amt > 0:
            rest = remaining - amt
            released = rest
            back_c = min(rest, int(a["held_credit"]))
            if back_c:
                post.append((f"holds:{aid}", f"credit:{a['credit_id']}", back_c, asset(ccy)))
            if rest - back_c:
                post.append((f"holds:{aid}", W(a['wallet']), rest - back_c, asset(ccy)))
        r = self.led.tx(f"auth:{aid}:capture:{seq}", post, metadata={"type": "card_capture"})
        self._crash("after_capture_tx")
        self.x("insert into auth_events values(?,?,?,?,?) on conflict do nothing", aid, "capture", seq, amt, released)
        if d.get("final", True):
            self.x("update auths set status='captured' where auth_id=?", aid)
        self.audit("card.capture", aid, {"amount": amt, "released": released})
        return {"auth_id": aid, "captured": amt, "released": released, "ledger_tx": r["id"], "duplicate": r["dup"]}

    def card_reverse(self, d, now):
        aid = self._id(d, "auth_id")
        a = self._auth(aid)
        if a["status"] == "reversed":
            return {"auth_id": aid, "status": "reversed", "released": self._auth_totals(aid)[1], "duplicate": True}
        if a["status"] != "authorized":
            return {"auth_id": aid, "status": a["status"], "released": 0}
        ccy = a["ccy"]
        captured, released_tot = self._auth_totals(aid)
        remaining = int(a["held_wallet"]) + int(a["held_credit"]) - captured - released_tot
        post, back_c = [], min(remaining, int(a["held_credit"]))
        if back_c:
            post.append((f"holds:{aid}", f"credit:{a['credit_id']}", back_c, asset(ccy)))
        if remaining - back_c:
            post.append((f"holds:{aid}", W(a['wallet']), remaining - back_c, asset(ccy)))
        if post:
            self.led.tx(f"auth:{aid}:reverse", post, metadata={"type": "card_reverse"})
        self.x("insert into auth_events values(?,?,?,?,?) on conflict do nothing", aid, "reverse", "1", 0, remaining)
        self.x("update auths set status='reversed' where auth_id=?", aid)
        self.audit("card.reverse", aid, {"released": remaining})
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
            raise NotFound("unknown quote")
        if qt["status"] == "used":
            return {"status": "converted", "duplicate": True, "buy": qt["buy"]}
        if iso(now) > qt["expires"]:
            raise Declined("quote expired")
        try:
            r = self.led.tx(f"fx:{qt['id']}", [(W(qt['wallet']), f"fx:pool:{qt['sell_ccy']}", qt["sell"], asset(qt["sell_ccy"])),
                                              (f"fx:pool:{qt['buy_ccy']}", W(qt['wallet']), qt["buy"], asset(qt["buy_ccy"]))],
                            metadata={"type": "fx", "rate": qt["rate"]})
        except InsufficientFunds:
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
        a = asset(d["ccy"])
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
        self.x("insert into facilities values(?,?,?,?,?,?,?) on conflict do nothing", d["facility_id"], d["borrower"], d["ccy"], int(d["limit"]),
               int(d.get("apr_bps", 0)), "active", iso(now)[:10])
        return {"facility_id": d["facility_id"], "limit": int(d["limit"]), "status": "active"}

    def _fac(self, fid):
        f = self.q1("select * from facilities where id=?", fid)
        if not f:
            raise NotFound(f"unknown facility {fid}")
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
                            vars={"credit": f"credit:{f['id']}", "wallet": W(f['borrower']), "amt": f"{a} {int(d['amount'])}",
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
            r = self.led.tx(f"credit:{f['id']}:repay:{d['ref']}", [(W(f['borrower']), f"credit:{f['id']}", amt, asset(f["ccy"]))],
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
                self.led.tx(f"guarantee:{gid}:collateral", [(W(d['applicant']), f"collateral:{gid}", collateral, a)],
                            metadata={"type": "guarantee_collateral"})
            except InsufficientFunds:
                raise Declined("insufficient funds for collateral")
        self.led.tx(f"guarantee:{gid}:memo", script=NS_MEMO,
                    vars={"issued": f"memo:guarantees:issued:{d['ccy']}", "memo": f"memo:guarantee:{gid}", "amt": f"{a} {amt}"},
                    metadata={"type": "guarantee_memo"})
        self.x("insert into guarantees values(?,?,?,?,?,?,0,?,?) on conflict do nothing", gid, d["applicant"], d["beneficiary"], d["ccy"], amt,
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
                        vars={"collateral": f"collateral:{gid}", "applicant": W(g['applicant']),
                              "receivable": f"receivable:guarantee:{gid}", "beneficiary": W(g['beneficiary']), "amt": f"{a} {amt}"},
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
            self.led.tx(f"guarantee:{gid}:release", [(f"collateral:{gid}", W(g['applicant']), left, a)])
        self.x("update guarantees set status='released' where id=?", gid)
        return {"guarantee_id": gid, "collateral_returned": left, "status": "released"}

    # ------------------------------------------------------------------------------------------- inbound & reconciliation
    # --- operator-assisted banks (type "manual"): the human-in-the-loop leg of a live channel ---------------------------
    def _manual(self, name):
        b = self.banks.get(name)
        if b is None or not hasattr(b, "mark_sent"):
            raise NotFound(f"{name} is not an operator-assisted bank")
        return b

    def manual_instructions(self, name, status=None):
        return {"bank": name, "instructions": self._manual(name).instructions(status)}

    def manual_sent(self, name, payment_id, d):
        r = self._manual(name).mark_sent(payment_id, str((d or {}).get("sent_ref", "")))
        self.audit("manual_sent", payment_id, r)
        return r

    def manual_failed(self, name, payment_id, d):
        try:
            r = self._manual(name).mark_failed(payment_id, str((d or {}).get("reason", "operator")))
        except ValueError as ex:
            raise Declined(str(ex))
        self.audit("manual_failed", payment_id, r)
        return r

    def manual_statement(self, name, now, d):
        d = d or {}
        if d.get("format") not in ("csv", "bai2") or not isinstance(d.get("content"), str):
            raise ValidationError("body needs format ('csv'|'bai2') and content (string)")
        try:
            r = self._manual(name).ingest(d["format"], d["content"])
        except ValueError as ex:
            raise ValidationError(str(ex))
        r["advanced"] = self.poll_payouts(now)       # settle/fail payouts the new statement resolved
        r["reconcile"] = self.reconcile(name, now)
        self.audit("manual_statement", name, {k: r[k] for k in ("lines", "new", "settled")})
        return r

    def apply_inbound(self, bank, e):
        """Post a bank credit to the Ledger (idempotent on bank_ref). Attributable via narrative 'WALLET:<id>', else suspense."""
        self._need(e, "bank_ref", "ccy", "amount", "narrative")
        a = asset(e["ccy"])
        wid = e["narrative"].split("WALLET:")[1].split()[0] if "WALLET:" in e["narrative"] else None
        dest = W(wid) if wid else f"unapplied:{bank}:{e['ccy']}"
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
                self.x("insert into recon values(?,?,?,?,?) on conflict(bank, bank_ref) do update set kind=excluded.kind, status=excluded.status, detail=excluded.detail", bank_name, ref, kind, "open", json.dumps(it))
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
            v, a = amt("debitAmount"); post = [(W(wa), f"rafiki:inflight:{pid}", v, a)]
        elif t == "outgoing_payment.completed":
            v, a = amt("sentAmount"); post = [(f"rafiki:inflight:{pid}", "rafiki:settlement", v, a)]
        elif t == "outgoing_payment.failed":
            v, a = amt("debitAmount"); post = [(f"rafiki:inflight:{pid}", W(wa), v, a)]
        elif t == "incoming_payment.completed":
            v, a = amt("receivedAmount"); post = [("rafiki:settlement", W(wa), v, a)]
        else:
            return {"ignored": t}
        try:
            r = self.led.tx(f"rafiki:{ev['id']}", post, metadata={"type": t})
        except InsufficientFunds:
            raise Declined("insufficient funds for Rafiki outgoing payment")
        return {"posted": t, "ledger_tx": r["id"], "duplicate": r["dup"]}

    # ------------------------------------------------------------------------------------------- Stripe Issuing
    # issuing_authorization.request -> hold on the card's wallet (card_authorize). issuing_transaction.created -> capture into
    # card:clearing (hold first, wallet for any excess/force capture) or refund out of it. issuing_authorization.updated
    # (closed|reversed|expired) -> release the remaining hold. Ledger reference stripe:<object id> makes every event replay-safe.
    def stripe_issuing_authorization(self, ev, now):
        o = ev["data"]["object"]
        pr, md = o["pending_request"], o.get("merchant_data") or {}
        card = o["card"]["id"] if isinstance(o["card"], dict) else o["card"]
        if int(pr["amount"]) == 0:
            c = self.q1("select status from cards where card_id=?", card)
            return {"approved": bool(c and c["status"] == "active")}
        r = self.card_authorize({"auth_id": o["id"], "card_id": card, "amount": int(pr["amount"]), "ccy": pr["currency"].upper(),
                                 "merchant": md.get("name"), "mcc": md.get("category_code"), "country": md.get("country")}, now)
        return {"approved": r["approved"], "metadata": {"response_code": r["response_code"], "reason": r["reason"]}}

    def stripe_issuing_event(self, ev, now):
        t, o = ev["type"], ev["data"]["object"]
        if t == "issuing_transaction.created":
            return self._stripe_transaction(o)
        if t == "issuing_authorization.updated" and o.get("status") in ("closed", "reversed", "expired"):
            if not self.q1("select 1 from auths where auth_id=?", o["id"]):
                return {"ignored": t, "reason": "no hold for authorization"}
            return self.card_reverse({"auth_id": o["id"]}, now)
        return {"ignored": t}

    def _stripe_transaction(self, o):
        iid, amount, ccy = o["id"], int(o["amount"]), o["currency"].upper()
        cid = o["card"]["id"] if isinstance(o["card"], dict) else o["card"]
        card = self.q1("select wallet from cards where card_id=?", cid)
        if not card:
            raise NotFound(f"unknown card {cid}")
        ref, wal, a, clearing = f"stripe:{iid}", W(card["wallet"]), asset(ccy), f"card:clearing:{ccy}"
        aid = o.get("authorization")
        aid = aid["id"] if isinstance(aid, dict) else aid
        existing = self.led.tx_by_reference(ref)
        if existing is None:
            try:
                if o["type"] == "capture" and amount < 0:
                    amt, from_hold = -amount, 0
                    auth = self.q1("select * from auths where auth_id=?", aid) if aid else None
                    if auth and auth["status"] == "authorized":
                        c, rel = self._auth_totals(aid)
                        from_hold = min(amt, int(auth["held_wallet"]) + int(auth["held_credit"]) - c - rel)
                    post = ([(f"holds:{aid}", clearing, from_hold, a)] if from_hold else []) + \
                           ([(wal, clearing, amt - from_hold, a)] if amt > from_hold else [])
                    r = self.led.tx(ref, post, metadata={"type": "card_capture", "source": "stripe_issuing", "card": cid})
                elif o["type"] == "refund" and amount > 0:
                    r = self.led.tx(ref, script=NS_CLEARING_CREDIT, vars={"clearing": clearing, "wallet": wal, "amt": f"{a} {amount}"},
                                    metadata={"type": "card_refund", "source": "stripe_issuing", "card": cid})
                elif o["type"] == "refund" and amount < 0:
                    r = self.led.tx(ref, [(wal, clearing, -amount, a)], metadata={"type": "card_refund_reversal", "source": "stripe_issuing",
                                                                                 "card": cid})
                else:
                    raise ValidationError(f"unsupported issuing transaction type={o['type']} amount={amount}")
            except InsufficientFunds:
                raise Declined("insufficient funds for captured card spend")
            existing = {"id": r["id"], "postings": r["postings"] or (self.led.tx_by_reference(ref) or {}).get("postings") or []}
        held = sum(int(p["amount"]) for p in existing["postings"] if p["source"] == f"holds:{aid}") if aid else 0
        if held:
            self.x("insert into auth_events values(?,?,?,?,?) on conflict do nothing", aid, "capture", iid, held, 0)
        self.audit("card.stripe_transaction", iid, {"card": cid, "type": o["type"], "amount": amount, "from_hold": held})
        return {"transaction": iid, "type": o["type"], "ledger_tx": existing["id"], "from_hold": held}
