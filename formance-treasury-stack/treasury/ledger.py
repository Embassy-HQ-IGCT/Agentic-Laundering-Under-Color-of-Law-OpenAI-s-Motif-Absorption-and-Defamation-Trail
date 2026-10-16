"""Thin REST client for Formance Ledger v3 (HTTP :9000). Stdlib only, one keep-alive connection per thread.

Every money movement goes through tx(): a single atomic ledger transaction identified by a unique `reference`.
A repeated reference is NOT an error for callers: Ledger answers 409 TRANSACTION_REFERENCE_CONFLICT and we report dup=True,
which is what makes every API operation in this service safely retryable.
"""
import http.client
import json
import re
import socket
import threading
import urllib.parse


class LedgerError(Exception):
    def __init__(self, status, code, message):
        super().__init__(f"{status} {code}: {message}")
        self.status, self.code, self.message = status, code, message


class InsufficientFunds(LedgerError):
    pass


class Ledger:
    def __init__(self, base="http://127.0.0.1:9000", name="treasury"):
        u = urllib.parse.urlparse(base)
        self.host, self.port, self.name = u.hostname, u.port or 80, name
        self._tl = threading.local()

    # -- transport ---------------------------------------------------------------------------------------------
    def _conn(self):
        c = getattr(self._tl, "c", None)
        if c is None:
            c = self._tl.c = http.client.HTTPConnection(self.host, self.port, timeout=15)
            c.connect()
            c.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)   # small split writes + delayed ACK = ~40ms stalls otherwise
        return c

    def _req(self, method, path, body=None):
        headers = {"Content-Type": "application/json", "Formance-Bigint-As-String": "true"}
        data = json.dumps(body) if body is not None else None
        for attempt in (0, 1):
            try:
                c = self._conn()
                c.request(method, path, body=data, headers=headers)
                r = c.getresponse()
                raw = r.read()
                break
            except (http.client.HTTPException, OSError):
                self._tl.c = None  # stale keep-alive; retry once on a fresh connection
                if attempt:
                    raise
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {"errorCode": "BAD_RESPONSE", "errorMessage": raw[:200].decode("utf-8", "replace")}
        return r.status, payload

    # -- setup -------------------------------------------------------------------------------------------------
    def ensure(self):
        status, p = self._req("POST", f"/v3/{self.name}", {})
        if status not in (200, 201, 409):
            raise LedgerError(status, p.get("errorCode"), p.get("errorMessage"))

    # -- writes ------------------------------------------------------------------------------------------------
    def tx(self, ref, postings=None, script=None, vars=None, metadata=None, force=False):
        """Create one atomic transaction. Returns {"id", "postings", "dup"}. Raises InsufficientFunds / LedgerError."""
        body = {"reference": ref}
        if postings is not None:
            body["postings"] = [
                {"source": s, "destination": d, "amount": int(a), "asset": asset} for (s, d, a, asset) in postings
            ]
        else:
            body["script"] = {"plain": script, "vars": {k: str(v) for k, v in (vars or {}).items()}}
        if metadata:
            body["metadata"] = {k: str(v) for k, v in metadata.items()}
        if force:
            body["force"] = True
        status, p = self._req("POST", f"/v3/{self.name}/transactions", body)
        if status in (200, 201):
            t = p["data"]["transaction"]
            return {"id": t["id"], "postings": t["postings"], "dup": False}
        code, msg = p.get("errorCode", ""), p.get("errorMessage", "")
        if status == 409 and code == "TRANSACTION_REFERENCE_CONFLICT":
            m = re.search(r"existingTransactionId=\"?(\d+)", msg)
            return {"id": int(m.group(1)) if m else None, "postings": None, "dup": True}
        if code == "INSUFFICIENT_FUNDS":
            raise InsufficientFunds(status, code, msg)
        raise LedgerError(status, code, msg)

    # -- reads -------------------------------------------------------------------------------------------------
    def balance(self, account, asset):
        status, p = self._req("GET", f"/v3/{self.name}/accounts/{urllib.parse.quote(account, safe=':')}")
        if status == 404:
            return 0
        if status != 200:
            raise LedgerError(status, p.get("errorCode"), p.get("errorMessage"))
        for v in p["data"].get("volumes", []):
            if v["asset"] == asset and v.get("color", "") == "":
                return int(v["volumes"]["balance"])
        return 0

    def tx_by_reference(self, ref):
        flt = urllib.parse.quote(json.dumps({"$match": {"reference": ref}}))
        status, p = self._req("GET", f"/v3/{self.name}/transactions?pageSize=1&filter={flt}")
        if status != 200:
            return None
        data = p.get("data") or p.get("cursor", {}).get("data") or []
        return data[0] if data else None

    def all_balances(self, asset):
        """Sum of every account's balance for an asset (must be 0 in a closed double-entry ledger). Pages the accounts list."""
        total, accounts, cursor = 0, {}, None
        while True:
            q = "pageSize=100" + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else "")
            status, p = self._req("GET", f"/v3/{self.name}/accounts?{q}")
            if status != 200:
                raise LedgerError(status, p.get("errorCode"), p.get("errorMessage"))
            rows = p.get("data") or []
            for a in rows:
                for v in a.get("volumes", []):
                    if v["asset"] == asset:
                        b = int(v["volumes"]["balance"])
                        accounts[a["address"]] = accounts.get(a["address"], 0) + b
                        total += b
            cursor = p.get("next") if p.get("hasMore") else None
            if not cursor:
                break
        if "world" not in accounts:       # the virtual source/sink account is not listed; include it so the sum must be exactly 0
            w = self.balance("world", asset)
            accounts["world"] = w
            total += w
        return total, accounts
