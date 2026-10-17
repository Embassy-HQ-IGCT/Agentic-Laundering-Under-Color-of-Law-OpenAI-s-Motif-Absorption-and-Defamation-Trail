# SPDX-License-Identifier: Apache-2.0
"""Operator-assisted bank adapter ("manual" bank type) - a LIVE channel that needs no bank API.

Use it while a bank account has no API/CashPro entitlement yet (e.g. a BofA business account with online-banking wires):

  engine.submit()  -> writes an instruction row (idempotent on payment id) and returns "accepted"
  operator         -> reads the instruction (GET /bank/<name>/instructions or bankops.py), sends the wire in the bank's portal with
                      the reference text  PAY:<payment id>  and (optionally) records the bank's IMAD/OMAD with mark_sent()
  statement ingest -> CSV or BAI2 downloaded from the bank. A debit whose text contains PAY:<id> and whose amount equals the
                      instruction settles the payout; unmatched debits surface in reconciliation, never silently absorbed.
  mark_failed()    -> the operator could not send it: the payout fails and the engine releases the reserved liquidity.

It honours the BankAdapter contract (idempotent submit, lookup, honest status) so crash recovery works unchanged. When the bank's
API becomes available, swap the bank type; ledger, routing and reconciliation logic do not change.
State lives in the engine's Store (SQLite/Postgres), so every replica sees the same queue.
"""
import csv
import datetime as dt
import hashlib
import io
import json
import re
from decimal import Decimal, InvalidOperation

from banks import AdapterError, BankAdapter, Rejected

SCHEMA = [
    """create table if not exists manual_instr(payment_id text primary key, bank text, rail text, ccy text, amount bigint, dest text,
       bank_ref text, status text, sent_ref text, reason text, created text, updated text)""",
    """create table if not exists manual_stmt(bank text, bank_ref text, direction text, amount bigint, ccy text, value_date text,
       narrative text, rail text, primary key(bank, bank_ref))""",
    """create table if not exists manual_bal(bank text, ccy text, balance bigint, as_of text, primary key(bank, ccy))""",
]
_PAY = re.compile(r"PAY:([A-Za-z0-9_.\-]+)")
EXP = {"JPY": 0, "KRW": 0}          # minor-unit exponents other than 2


def _minor(text, ccy):
    exp = EXP.get(ccy, 2)
    try:
        d = Decimal(re.sub(r"[,$\s]", "", str(text)).replace("(", "-").replace(")", ""))
    except InvalidOperation:
        raise ValueError(f"bad amount {text!r}")
    return int((d * (10 ** exp)).to_integral_value())


def _iso_date(s, fmt=None):
    s = s.strip()
    for f in ([fmt] if fmt else []) + ["%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%d-%b-%Y", "%Y%m%d"]:
        try:
            return dt.datetime.strptime(s, f).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"unrecognised date {s!r}")


def _line_ref(bank, date, amount, desc, ordinal):
    return "L" + hashlib.sha256(f"{bank}|{date}|{amount}|{desc}|{ordinal}".encode()).hexdigest()[:20]


class ManualBank(BankAdapter):
    def __init__(self, cfg):
        self.name = cfg["name"]
        self.rails = frozenset(cfg.get("rails", ["FEDWIRE", "SWIFT"]))
        self.ccys = frozenset(cfg.get("ccys", ["USD"]))
        self._opening = dict(cfg.get("opening") or {})     # minor units; used until a statement supplies a balance
        self.csv_map = dict({"date": "Date", "desc": "Description", "amount": "Amount", "debit": None, "credit": None,
                             "balance": "Running Bal.", "date_fmt": None, "ccy": "USD"}, **(cfg.get("csv") or {}))
        self.db = None

    def bind(self, store):
        self.db = store
        for s in SCHEMA:
            store.x(s)

    # ---------------------------------------------------------------------------------------------- BankAdapter
    def healthy(self):
        return bool(self.db) and self.db.ping()

    def balance(self, ccy):
        """Bank-reported available cash from the latest ingested statement (else configured opening), minus instructions we have
        queued that the bank has not yet debited."""
        row = self.db.q1("select balance from manual_bal where bank=? and ccy=?", self.name, ccy)
        base = row["balance"] if row else self._opening.get(ccy, 0)
        pend = self.db.q1("select coalesce(sum(amount),0) as s from manual_instr where bank=? and ccy=? and status='accepted'",
                          self.name, ccy)["s"]
        return int(base) - int(pend)

    def lookup(self, payment_id):
        r = self.db.q1("select bank_ref, status from manual_instr where payment_id=?", payment_id)
        return {"bank_ref": r["bank_ref"], "status": r["status"]} if r else None

    def submit(self, rail, payment, now):
        if rail not in self.rails:
            raise Rejected(f"{rail} not enabled for {self.name}")
        ccy, amt = payment["ccy"], payment["amount"]
        if ccy not in self.ccys and "*" not in self.ccys:
            raise Rejected(f"{ccy} not supported")
        pid = payment["id"]
        ref = f"{self.name.upper()}-{pid}"
        ts = now.isoformat()
        try:
            self.db.x("insert into manual_instr(payment_id,bank,rail,ccy,amount,dest,bank_ref,status,created,updated) "
                      "values(?,?,?,?,?,?,?,?,?,?) on conflict do nothing",
                      pid, self.name, rail, ccy, int(amt), json.dumps(payment.get("dest") or {}), ref, "accepted", ts, ts)
        except Exception as e:                       # DB trouble: the instruction may or may not be stored
            raise AdapterError(f"{self.name} instruction store error: {e}", sent=None)
        r = self.db.q1("select bank_ref, status from manual_instr where payment_id=?", pid)
        return {"bank_ref": r["bank_ref"], "status": r["status"], "eta": now}

    def status(self, bank_ref, now):
        r = self.db.q1("select status from manual_instr where bank_ref=?", bank_ref)
        return r["status"] if r else "failed"

    def statement(self, since=None):
        rows = self.db.q("select bank_ref,direction,amount,ccy,value_date,narrative,rail from manual_stmt where bank=? "
                         "order by value_date, bank_ref", self.name)
        return [r for r in rows if not since or r["value_date"] >= since]

    # ---------------------------------------------------------------------------------------------- operator actions
    def instructions(self, status=None):
        q = "select * from manual_instr where bank=?" + (" and status=?" if status else "") + " order by created"
        rows = self.db.q(q, self.name, *([status] if status else []))
        for r in rows:
            r["dest"] = json.loads(r["dest"] or "{}")
            r["reference_text"] = f"PAY:{r['payment_id']}"       # put this in the wire's reference / OBI field
        return rows

    def mark_sent(self, payment_id, sent_ref=""):
        """Operator sent it in the portal; record the bank's confirmation (IMAD/OMAD/confirmation no.). Status stays 'accepted'
        until the statement shows the debit."""
        n = self.db.x("update manual_instr set sent_ref=?, updated=? where payment_id=? and bank=? and status='accepted'",
                      sent_ref, dt.datetime.now(dt.timezone.utc).isoformat(), payment_id, self.name)
        if not n:
            raise ValueError("no open instruction for that payment")
        return {"payment_id": payment_id, "sent_ref": sent_ref}

    def mark_failed(self, payment_id, reason):
        """Operator could not send it (or it was cancelled before sending). Never use after sending: a sent wire is final."""
        r = self.db.q1("select sent_ref from manual_instr where payment_id=? and bank=? and status='accepted'", payment_id, self.name)
        if not r:
            raise ValueError("no open instruction for that payment")
        if r["sent_ref"]:
            raise ValueError("refusing to fail an instruction already recorded as sent; wait for the statement or investigate")
        self.db.x("update manual_instr set status='failed', reason=?, updated=? where payment_id=? and bank=?",
                  reason, dt.datetime.now(dt.timezone.utc).isoformat(), payment_id, self.name)
        return {"payment_id": payment_id, "status": "failed"}

    # ---------------------------------------------------------------------------------------------- statement ingest
    def ingest(self, fmt, content):
        entries, balances = (parse_bai2(content, self.name) if fmt == "bai2" else self._parse_csv(content))
        settled, added = [], 0
        for e in entries:
            m = _PAY.search(e["narrative"]) if e["direction"] == "debit" else None
            ins = self.db.q1("select * from manual_instr where payment_id=? and bank=?", m.group(1), self.name) if m else None
            if ins:
                e["bank_ref"], e["rail"] = ins["bank_ref"], ins["rail"]
            n = self.db.x("insert into manual_stmt values(?,?,?,?,?,?,?,?) on conflict do nothing",
                          self.name, e["bank_ref"], e["direction"], e["amount"], e["ccy"], e["value_date"], e["narrative"], e["rail"])
            added += 1 if n else 0
            if ins and ins["status"] == "accepted" and ins["amount"] == e["amount"] and ins["ccy"] == e["ccy"]:
                self.db.x("update manual_instr set status='settled', updated=? where payment_id=?",
                          dt.datetime.now(dt.timezone.utc).isoformat(), ins["payment_id"])
                settled.append(ins["payment_id"])
        for ccy, (bal, as_of) in balances.items():
            self.db.x("insert into manual_bal values(?,?,?,?) on conflict(bank, ccy) do update set balance=excluded.balance, as_of=excluded.as_of "
                      "where excluded.as_of >= manual_bal.as_of", self.name, ccy, bal, as_of)
        return {"lines": len(entries), "new": added, "settled": settled, "balances": {c: b[0] for c, b in balances.items()}}

    def _parse_csv(self, content):
        m, ccy = self.csv_map, self.csv_map["ccy"]
        text = content.lstrip("﻿")
        lines = text.splitlines()
        hdr = next((i for i, l in enumerate(lines) if m["date"] in l and (m["desc"] in l)), None)     # bank CSVs often have a summary header
        if hdr is None:
            raise ValueError(f"CSV header with columns {m['date']!r} and {m['desc']!r} not found")
        out, seen, bal_row = [], {}, None
        for row in csv.DictReader(io.StringIO("\n".join(lines[hdr:]))):
            if not row.get(m["date"]) or not row.get(m["desc"]):
                continue
            try:
                date = _iso_date(row[m["date"]], m["date_fmt"])
            except ValueError:
                continue                                  # beginning/ending balance summary rows
            if m["debit"] or m["credit"]:
                d, c = row.get(m["debit"]) or "", row.get(m["credit"]) or ""
                amt = _minor(c, ccy) if c.strip() else -abs(_minor(d, ccy)) if d.strip() else 0
            else:
                amt = _minor(row[m["amount"]], ccy)
            if amt == 0:
                continue
            desc = " ".join(row[m["desc"]].split())
            key = (date, amt, desc)
            seen[key] = seen.get(key, 0) + 1
            out.append({"bank_ref": _line_ref(self.name, date, amt, desc, seen[key]), "direction": "credit" if amt > 0 else "debit",
                        "amount": abs(amt), "ccy": ccy, "value_date": date, "narrative": desc, "rail": "FEDWIRE"})
            if m["balance"] and row.get(m["balance"], "").strip():
                bal_row = (_minor(row[m["balance"]], ccy), date)
        return out, ({ccy: bal_row} if bal_row else {})


# ------------------------------------------------------------------------------------------------ BAI2
def _bai_logical(content):
    recs, cur = [], None
    for raw in content.replace("\r", "").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("88,") and cur is not None:
            cur += "," + line[3:].rstrip("/")
            continue
        if cur is not None:
            recs.append(cur)
        cur = line.rstrip("/")
    if cur is not None:
        recs.append(cur)
    return recs


def parse_bai2(content, bank):
    """Minimal BAI2: 02 group (date, currency), 03 account (balances), 16 transactions (type 100-399 credit, 400-699 debit)."""
    out, balances, seen = [], {}, {}
    date, ccy = None, "USD"
    for rec in _bai_logical(content):
        f = rec.split(",")
        code = f[0]
        if code == "02":
            ccy = (f[6] if len(f) > 6 and f[6] else "USD")
            date = dt.datetime.strptime(f[4], "%y%m%d").date().isoformat() if len(f) > 4 and f[4] else date
        elif code == "03":
            rest = f[3:]                       # (type, amount, item count, funds type)* ; 045/015 closing, 040 opening available
            i = 0
            want = {"045": 3, "015": 2, "040": 1}
            best = balances.get(ccy, (None, None, 0))[2]
            while i + 1 < len(rest):
                t, a = rest[i], rest[i + 1]
                if t in want and a and want[t] > best:
                    balances[ccy] = (_minor(Decimal(a) / 100, ccy), date, want[t])
                    best = want[t]
                i += 4
        elif code == "16":
            t = int(f[1]) if f[1].isdigit() else 0
            if not (100 <= t <= 699):
                continue
            amt = int(f[2])
            bankref = f[4] if len(f) > 4 and f[4].strip() else ""
            text = " ".join(f[6:]).strip() if len(f) > 6 else ""
            custref = f[5].strip() if len(f) > 5 else ""
            narrative = " ".join(f"{custref} {text}".split())
            key = (date, amt, t, narrative)
            seen[key] = seen.get(key, 0) + 1
            ref = ("B" + bankref) if bankref and bankref != "0" else _line_ref(bank, date, amt, narrative, seen[key])
            out.append({"bank_ref": ref, "direction": "credit" if t < 400 else "debit", "amount": amt, "ccy": ccy,
                        "value_date": date, "narrative": narrative, "rail": "FEDWIRE"})
    return out, {c: (b[0], b[1]) for c, b in balances.items()}
