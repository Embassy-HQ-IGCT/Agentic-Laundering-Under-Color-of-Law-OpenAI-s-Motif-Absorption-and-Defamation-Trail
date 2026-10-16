"""Bank adapter interface + a faithful mock, plus production adapter stubs.

The engine never talks to a bank except through BankAdapter. To go live you implement the same methods against the bank's
interface (CashPro APIs / SFTP file exchange / FedNow-RTP service provider APIs) and select it in the bank config.
MockBank enforces the same rules the router relies on (rail windows, limits, liquidity, instruction idempotency) so routing and
crash-recovery logic is tested honestly.
"""
import datetime as dt
import itertools
import json
import os
import threading

import rails as railmod


class AdapterError(Exception):
    """Failure talking to the bank. `sent` says whether the instruction may have left us:
    sent=False -> provably not sent (connection refused): safe to fail over to another bank.
    sent=None  -> unknown (timeout after write): NEVER fail over blindly - double-pay risk. Park for lookup / review."""

    def __init__(self, message, sent=None):
        super().__init__(message)
        self.sent = sent


class Rejected(Exception):
    """Definitive refusal. Nothing was sent; safe to release the reservation and try another route."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class BankAdapter:
    """CONTRACT (production adapters MUST honour this; crash recovery and failover safety depend on it):
      * submit() is IDEMPOTENT on payment["id"]: pass it to the bank as the unique instruction / end-to-end id, and if the bank already
        has that id return the EXISTING instruction (same bank_ref, current status) without sending again or re-validating.
      * lookup(payment_id) -> {"bank_ref","status"} if the bank has the instruction, None if it DEFINITIVELY does not.
      * Raise Rejected only for a definitive refusal (nothing sent). Raise AdapterError(sent=False) only if the instruction provably
        never left; any ambiguity (timeout after write) must be AdapterError(sent=None).
      * status values: "settled" | "accepted" (in flight) | "failed".
    """
    name = "?"
    rails = frozenset()
    ccys = frozenset()

    def healthy(self): raise NotImplementedError
    def balance(self, ccy): raise NotImplementedError                 # available cash, minor units, as the bank reports it
    def lookup(self, payment_id): raise NotImplementedError
    def submit(self, rail, payment, now): raise NotImplementedError   # -> {"bank_ref","status","eta"}
    def status(self, bank_ref, now): raise NotImplementedError
    def statement(self, since=None): raise NotImplementedError        # -> [entry]; entry: bank_ref,direction,amount,ccy,value_date,narrative,rail


class MockBank(BankAdapter):
    def __init__(self, name, rails, ccys, opening=None, limits=None):
        self.name, self.rails, self.ccys = name, frozenset(rails), frozenset(ccys)
        self._bal = dict(opening or {})
        self._pending = {}            # bank_ref -> (ccy, amount, eta, payment)
        self._stmt, self._seq = [], itertools.count(1)
        self._lock = threading.RLock()
        self._by_payment = {}         # payment id -> {bank_ref, rail, eta}   (bank-side idempotency index)
        self.submissions = {}         # payment id -> instructions actually created (tests assert exactly-once)
        self.down = False             # healthy() False; every call raises sent=False
        self.fail_next = []           # reasons to reject the next submissions with
        self.refuse_connect = False   # healthy() True but the connection is refused at submit (provably not sent)
        self.timeout_after_send = False   # bank accepted the instruction but the reply is lost: caller sees outcome UNKNOWN
        self.timeout_before_send = False  # request lost before the bank saw it: caller sees outcome UNKNOWN
        self._rails_cfg, self._hol = railmod.load_rails(), railmod.holidays()
        self.limits = limits or {}

    # --- test controls ---------------------------------------------------------------------------------------
    def inject_inbound(self, ccy, amount, narrative, rail="FEDWIRE", now=None):
        with self._lock:
            ref = f"{self.name.upper()}-IN-{next(self._seq):06d}"
            self._bal[ccy] = self._bal.get(ccy, 0) + amount
            self._stmt.append({"bank_ref": ref, "direction": "credit", "amount": amount, "ccy": ccy, "rail": rail,
                               "value_date": (now or dt.datetime.now(dt.timezone.utc)).date().isoformat(), "narrative": narrative})
            return ref

    def inject_debit(self, ccy, amount, narrative, now=None):
        """Test hook: a debit on the statement with no matching payout (what reconciliation must catch)."""
        with self._lock:
            ref = f"{self.name.upper()}-DR-{next(self._seq):06d}"
            self._bal[ccy] = self._bal.get(ccy, 0) - amount
            self._stmt.append({"bank_ref": ref, "direction": "debit", "amount": amount, "ccy": ccy, "rail": "FEDWIRE",
                               "value_date": (now or dt.datetime.now(dt.timezone.utc)).date().isoformat(), "narrative": narrative})
            return ref

    # --- BankAdapter -----------------------------------------------------------------------------------------
    def healthy(self):
        return not self.down

    def balance(self, ccy):
        if self.down:
            raise AdapterError(f"{self.name} unreachable", sent=False)
        with self._lock:
            return self._bal.get(ccy, 0) - sum(a for (c, a, _, _) in self._pending.values() if c == ccy)

    def lookup(self, payment_id):
        if self.down or self.refuse_connect:
            raise AdapterError(f"{self.name} unreachable", sent=False)
        with self._lock:
            known = self._by_payment.get(payment_id)
            if not known:
                return None
            return {"bank_ref": known["bank_ref"], "status": self._status_locked(known["bank_ref"], dt.datetime.now(dt.timezone.utc))}

    def submit(self, rail, payment, now):
        if self.down or self.refuse_connect:
            raise AdapterError(f"{self.name} unreachable", sent=False)
        with self._lock:
            known = self._by_payment.get(payment["id"])
            if known:      # idempotent: the same instruction with its current status; nothing is sent again
                return {"bank_ref": known["bank_ref"], "status": self._status_locked(known["bank_ref"], now), "eta": known["eta"]}
            if self.timeout_before_send:
                raise AdapterError(f"{self.name} timed out (request lost before the bank saw it)", sent=None)
            ccy, amt = payment["ccy"], payment["amount"]
            if self.fail_next:
                raise Rejected(self.fail_next.pop(0))
            if rail not in self.rails:
                raise Rejected(f"{rail} not enabled for {self.name}")
            if ccy not in self.ccys and "*" not in self.ccys:
                raise Rejected(f"{ccy} not supported")
            spec = self._rails_cfg[rail]
            if amt > self.limits.get(rail, spec["max_minor"]):
                raise Rejected("amount exceeds rail limit")
            av = railmod.availability(rail, now, self._rails_cfg, self._hol)
            if rail == "FEDWIRE" and not av["open"]:
                raise Rejected("FEDWIRE closed")
            avail = self._bal.get(ccy, 0) - sum(a for (c, a, _, _) in self._pending.values() if c == ccy)
            if amt > avail:
                raise Rejected("insufficient prefunded liquidity at bank")
            ref = f"{self.name.upper()}-{rail}-{next(self._seq):06d}"
            self.submissions[payment["id"]] = self.submissions.get(payment["id"], 0) + 1
            self._by_payment[payment["id"]] = {"bank_ref": ref, "rail": rail, "eta": av["eta"]}
            if av["instant"]:
                self._bal[ccy] -= amt
                self._debit(ref, payment, rail, now)
                res = {"bank_ref": ref, "status": "settled", "eta": now}
            else:
                self._pending[ref] = (ccy, amt, av["eta"], dict(payment, rail=rail))
                res = {"bank_ref": ref, "status": "accepted", "eta": av["eta"]}
            if self.timeout_after_send:
                raise AdapterError(f"{self.name} timed out after send (reply lost)", sent=None)
            return res

    def status(self, bank_ref, now):
        with self._lock:
            return self._status_locked(bank_ref, now)

    def _status_locked(self, bank_ref, now):
        if bank_ref in self._pending:
            ccy, amt, eta, payment = self._pending[bank_ref]
            if now >= eta:
                del self._pending[bank_ref]
                self._bal[ccy] -= amt
                self._debit(bank_ref, payment, payment["rail"], now)
                return "settled"
            return "accepted"
        return "settled" if any(e["bank_ref"] == bank_ref for e in self._stmt) else "failed"

    def statement(self, since=None):
        with self._lock:
            return [dict(e) for e in self._stmt if not since or e["value_date"] >= since]

    def _debit(self, ref, payment, rail, now):
        self._stmt.append({"bank_ref": ref, "direction": "debit", "amount": payment["amount"], "ccy": payment["ccy"], "rail": rail,
                           "value_date": now.date().isoformat(), "narrative": f"PAY:{payment['id']}"})


class BofACashProAdapter(BankAdapter):
    """PRODUCTION STUB - Bank of America. Implement against the interfaces BofA enables for your account
    (CashPro API / CashPro Connect for payments and account reporting; FedNow/RTP via BofA's real-time payments service;
    SWIFT via CashPro International). Needs your credentials, entitlements and BofA's API specification - none available here."""
    name = "bofa"

    def __init__(self, *a, **k):
        raise NotImplementedError("BofACashProAdapter requires BofA API credentials and spec; use type=mock until then")


class NavyFederalAdapter(BankAdapter):
    """PRODUCTION STUB - Navy Federal Credit Union. Confirm first that NFCU offers business treasury APIs / FedNow-RTP access for
    your account type; implement the BankAdapter methods against whatever interface they provide (API, SFTP or portal export)."""
    name = "nfcu"

    def __init__(self, *a, **k):
        raise NotImplementedError("NavyFederalAdapter requires NFCU's interface details; use type=mock until then")


def load_banks():
    """BANKS_JSON env or default: two mock banks. NOTE the default rail sets are ASSUMPTIONS to exercise routing - confirm real
    participation per bank (bofa: FedNow/RTP/Fedwire/ACH/SWIFT per your account; nfcu: set from their actual services)."""
    cfg = json.loads(os.environ["BANKS_JSON"]) if os.environ.get("BANKS_JSON") else [
        {"name": "bofa", "type": "mock", "rails": ["FEDNOW", "RTP", "FEDWIRE", "ACH", "SWIFT"], "ccys": ["USD", "EUR", "GBP"],
         "opening": {"USD": 500_000_000, "EUR": 100_000_000, "GBP": 50_000_000}},
        {"name": "nfcu", "type": "mock", "rails": ["FEDNOW", "ACH"], "ccys": ["USD"], "opening": {"USD": 200_000_000}},
    ]
    banks = {}
    if any(c["type"] == "mock" for c in cfg) and os.environ.get("TREASURY_ALLOW_MOCK_BANKS") != "1":
        raise RuntimeError("refusing to start: bank config contains MOCK banks (payments would be simulated, not sent). "
                           "Configure real adapters, or set TREASURY_ALLOW_MOCK_BANKS=1 for sandbox/staging only.")
    for c in cfg:
        if c["type"] == "mock":
            banks[c["name"]] = MockBank(c["name"], c["rails"], c["ccys"], c.get("opening"), c.get("limits"))
        elif c["type"] == "bofa":
            banks[c["name"]] = BofACashProAdapter(c)
        elif c["type"] == "nfcu":
            banks[c["name"]] = NavyFederalAdapter(c)
        else:
            raise ValueError(f"unknown bank type {c['type']}")
    return banks
