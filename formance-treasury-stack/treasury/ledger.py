"""REST client for Formance Ledger v3 (HTTP :9000). Stdlib only, one keep-alive connection per thread, TCP_NODELAY.

Every money movement is ONE atomic ledger transaction identified by a unique `reference`:
  * A repeated reference is not an error: the Ledger answers 409 and we return dup=True (safe retries / crash recovery).
  * Each transaction also carries a request FINGERPRINT in its metadata. If a reference is re-used with a *different* request
    (different amount, accounts, ...), the retry is rejected with IdempotencyConflict instead of silently "succeeding".
  * Per-request context (actor, request id) set by the API layer is stamped into transaction metadata for audit.
"""
import hashlib
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


class IdempotencyConflict(Exception):
    """Same idempotency reference, different request. Never apply; surface as HTTP 409."""


class Ledger:
    def __init__(self, base="http://127.0.0.1:9000", name="treasury", timeout=10):
        u = urllib.parse.urlparse(base)
        self.host, self.port, self.name, self.timeout = u.hostname, u.port or 80, name, timeout
        self._tl = threading.local()

    # -- request context (audit) -------------------------------------------------------------------------------
    def set_context(self, **meta):
        self._tl.meta = {k: str(v) for k, v in meta.items() if v is not None}

    def clear_context(self):
        self._tl.meta = {}

    # -- transport ---------------------------------------------------------------------------------------------
    def _conn(self):
        c = getattr(self._tl, "c", None)
        if c is None:
            c = self._tl.c = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
            c.connect()
            c.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)   # split small writes + delayed ACK = ~40 ms stalls otherwise
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
                self._tl.c = None           # stale keep-alive or restart; retry once on a fresh connection
                if attempt:
                    raise
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {"errorCode": "BAD_RESPONSE", "errorMessage": raw[:200].decode("utf-8", "replace")}
        return r.status, payload

    def healthy(self):
        try:
            status, _ = self._req("GET", "/health")
            return status == 200
        except (http.client.HTTPException, OSError):
            return False

    # -- setup -------------------------------------------------------------------------------------------------
    def ensure(self):
        status, p = self._req("POST", f"/v3/{self.name}", {})
        if status not in (200, 201, 409):
            raise LedgerError(status, p.get("errorCode"), p.get("errorMessage"))

    # -- writes ------------------------------------------------------------------------------------------------
    @staticmethod
    def fingerprint(postings, script, vars):
        canon = json.dumps({"p": postings, "s": script, "v": vars}, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canon.encode()).hexdigest()[:32]

    def tx(self, ref, postings=None, script=None, vars=None, metadata=None, force=False):
        """Create one atomic transaction -> {"id", "postings", "dup"}. Raises InsufficientFunds, IdempotencyConflict, LedgerError."""
        body = {"reference": ref}
        plist = None
        if postings is not None:
            plist = [[s, d, int(a), asset] for (s, d, a, asset) in postings]
            body["postings"] = [{"source": s, "destination": d, "amount": a, "asset": asset} for (s, d, a, asset) in plist]
        else:
            vars = {k: str(v) for k, v in (vars or {}).items()}
            body["script"] = {"plain": script, "vars": vars}
        fp = self.fingerprint(plist, script, vars)
        meta = dict(getattr(self._tl, "meta", {}))
        meta.update({k: str(v) for k, v in (metadata or {}).items()})
        meta["fp"] = fp
        body["metadata"] = meta
        if force:
            body["force"] = True
        status, p = self._req("POST", f"/v3/{self.name}/transactions", body)
        if status in (200, 201):
            t = p["data"]["transaction"]
            return {"id": t["id"], "postings": t["postings"], "dup": False}
        code, msg = p.get("errorCode", ""), p.get("errorMessage", "")
        if status == 409 and code == "TRANSACTION_REFERENCE_CONFLICT":
            m = re.search(r"existingTransactionId=\\?\"?(\d+)", msg)
            existing = self.tx_by_id(m.group(1)) if m else self.tx_by_reference(ref)
            if existing is None:
                raise LedgerError(status, code, f"conflicting reference {ref!r} but the existing transaction could not be read")
            if (existing.get("metadata") or {}).get("fp") not in (None, fp):
                raise IdempotencyConflict(f"reference {ref!r} was already used for a different request")
            return {"id": existing["id"], "postings": existing.get("postings"), "dup": True}
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

    def tx_by_id(self, tx_id):
        status, p = self._req("GET", f"/v3/{self.name}/transactions/{int(tx_id)}")
        if status == 404:
            return None
        if status != 200:
            raise LedgerError(status, p.get("errorCode"), p.get("errorMessage"))
        return (p.get("data") or {}).get("transaction")

    def tx_by_reference(self, ref):
        """Exact-reference lookup using the ledger's `reference` index (REQUIRED - see require_reference_index()).
        Without that index the Ledger rejects the filter (INDEX_NOT_FOUND); the plain `reference=` query parameter is silently ignored
        and returns unrelated transactions, so it must never be used."""
        flt = urllib.parse.quote(json.dumps({"$match": {"reference": ref}}))
        status, p = self._req("GET", f"/v3/{self.name}/transactions?pageSize=2&filter={flt}")
        if status != 200:
            raise LedgerError(status, p.get("errorCode"), p.get("errorMessage"))
        for t in p.get("data") or []:
            if t.get("reference") == ref:        # never trust the server to have filtered exactly
                return t
        return None

    def require_reference_index(self):
        """Fail fast at startup if duplicate detection / crash recovery cannot work. Create it once per ledger:
        `ledgerctl indexes create --ledger <name> --type reference`."""
        status, p = self._req("GET", f"/v3/{self.name}/transactions?pageSize=1&filter="
                                     + urllib.parse.quote(json.dumps({"$match": {"reference": "__index_probe__"}})))
        if status != 200:
            raise RuntimeError(f"ledger {self.name!r} has no usable 'reference' index ({p.get('errorCode')}). "
                               f"Run: ledgerctl indexes create --ledger {self.name} --type reference   (then wait until it is READY)")

    def all_balances(self, asset):
        """Sum of every account's balance for an asset including the virtual `world` account (must be exactly 0)."""
        total, accounts, cursor = 0, {}, None
        while True:
            q = "pageSize=100" + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else "")
            status, p = self._req("GET", f"/v3/{self.name}/accounts?{q}")
            if status != 200:
                raise LedgerError(status, p.get("errorCode"), p.get("errorMessage"))
            for a in p.get("data") or []:
                for v in a.get("volumes", []):
                    if v["asset"] == asset:
                        b = int(v["volumes"]["balance"])
                        accounts[a["address"]] = accounts.get(a["address"], 0) + b
                        total += b
            cursor = p.get("next") if p.get("hasMore") else None
            if not cursor:
                break
        if "world" not in accounts:
            w = self.balance("world", asset)
            accounts["world"] = w
            total += w
        return total, accounts
