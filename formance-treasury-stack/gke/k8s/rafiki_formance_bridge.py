#!/usr/bin/env python3
"""Rafiki -> Formance Ledger bridge.

Receives Rafiki (account-servicing-entity) webhooks and posts double-entry transactions to a Formance Ledger v3.
Also serves /rates (Rafiki's EXCHANGE_RATES_URL). Idempotent: each webhook event id is the ledger tx reference,
so a Rafiki retry never double-posts. Returns 500 on ledger failure so Rafiki retries.

Posting model (asset code/scale taken from the event, e.g. USD/2):
  outgoing_payment.created    customers:<wa>            -> rafiki:inflight:<payment>   debitAmount   (funds reserved; rejected if insufficient)
  outgoing_payment.completed  rafiki:inflight:<payment> -> rafiki:settlement           sentAmount    (leaves the customer's side)
  incoming_payment.completed  rafiki:settlement         -> customers:<wa>              receivedAmount (credited to receiver)
  outgoing_payment.failed     rafiki:inflight:<payment> -> customers:<wa>              debitAmount   (reservation released)
For on-ledger transfers rafiki:settlement nets to zero; for external ILP payments it holds the amount owed to/from the peer.
"""
import json, os, subprocess, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LEDGERCTL = os.environ.get("LEDGERCTL", "ledgerctl")
LEDGER = os.environ.get("LEDGER_NAME", "rafiki")
INSECURE = os.environ.get("LEDGER_INSECURE", "true") == "true"
SERVER = os.environ.get("LEDGER_SERVER", "localhost:8888")
PORT = int(os.environ.get("BRIDGE_PORT", "3100"))
LOG = os.environ.get("WEBHOOK_LOG", "")
RATES = {"base": "USD", "rates": {"EUR": 0.92, "GBP": 0.79, "USD": 1.0}}
LOCK = threading.Lock()

def asset(a): return f"{a['assetCode']}/{a['assetScale']}"

def postings(ev):
    t, d = ev["type"], ev["data"]; wa = d.get("walletAddressId"); pid = d["id"]
    if t == "outgoing_payment.created":   return [(f"customers:{wa}", f"rafiki:inflight:{pid}", d["debitAmount"])]
    if t == "outgoing_payment.completed": return [(f"rafiki:inflight:{pid}", "rafiki:settlement", d["sentAmount"])]
    if t == "outgoing_payment.failed":    return [(f"rafiki:inflight:{pid}", f"customers:{wa}", d["debitAmount"])]
    if t == "incoming_payment.completed": return [("rafiki:settlement", f"customers:{wa}", d["receivedAmount"])]
    return []

def post(ref, src, dst, amt):
    cmd = [LEDGERCTL] + (["--insecure"] if INSECURE else []) + ["--server", SERVER, "tx", "create", "--ledger", LEDGER, "--reference", ref,
           "--posting", f"{src},{dst},{amt['value']},{asset(amt)}"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    out = p.stdout + p.stderr
    if p.returncode == 0: return True
    if "reference" in out.lower() and ("exist" in out.lower() or "conflict" in out.lower() or "duplicate" in out.lower()): return True  # idempotent replay
    sys.stderr.write(f"ledger error for {ref}: {out[:300]}\n"); return False

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body):
        b = json.dumps(body).encode(); self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        self._send(200, RATES) if self.path.startswith("/rates") else self._send(404, {})
    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        if not self.path.startswith("/webhooks"): return self._send(404, {})
        ev = json.loads(raw)
        if LOG: open(LOG, "a").write(raw + "\n")
        try:
            with LOCK:
                for i, (s, dst, amt) in enumerate(postings(ev)):
                    if not post(f"rafiki:{ev['id']}:{i}", s, dst, amt): return self._send(500, {"error": "ledger"})
            self._send(200, {})
        except Exception as e:
            sys.stderr.write(f"bridge error: {e}\n"); self._send(500, {"error": str(e)})

if __name__ == "__main__":
    ThreadingHTTPServer((os.environ.get("BRIDGE_BIND", "127.0.0.1"), PORT), H).serve_forever()
