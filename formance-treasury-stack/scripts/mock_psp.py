#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Mock PSP implementing the Formance generic-connector API. Bearer token must be 'test-key'."""
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs
T = "2026-10-01T10:00:00Z"
ACCOUNTS = [{"id": "acc-1", "accountName": "Operating", "createdAt": T}, {"id": "acc-2", "accountName": "Reserve", "createdAt": T}]
TXNS = [
 {"id": "tx-1", "createdAt": T, "updatedAt": T, "currency": "USD", "type": "PAYIN", "status": "SUCCEEDED", "amount": "10000", "destinationAccountID": "acc-1"},
 {"id": "tx-2", "createdAt": T, "updatedAt": T, "currency": "USD", "type": "PAYOUT", "status": "SUCCEEDED", "amount": "2500", "sourceAccountID": "acc-1"},
 {"id": "tx-3", "createdAt": T, "updatedAt": T, "currency": "USD", "type": "TRANSFER", "status": "SUCCEEDED", "amount": "1000", "sourceAccountID": "acc-1", "destinationAccountID": "acc-2"},
]
class H(BaseHTTPRequestHandler):
    def log_message(self, f, *a): sys.stderr.write("mock-psp %s\n" % (f % a))
    def send(self, code, body):
        b = json.dumps(body).encode(); self.send_response(code)
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer test-key": return self.send(401, {"message": "bad key"})
        u = urlparse(self.path); page = int(parse_qs(u.query).get("page", ["1"])[0]); p = u.path.rstrip("/")
        if p == "/accounts": return self.send(200, ACCOUNTS if page == 1 else [])
        if p == "/beneficiaries": return self.send(200, [])
        if p == "/transactions": return self.send(200, TXNS if page == 1 else [])
        if p.startswith("/accounts/") and p.endswith("/balances"):
            aid = p.split("/")[2]; amt = {"acc-1": "6500", "acc-2": "1000"}.get(aid, "0")
            return self.send(200, {"id": "bal-" + aid, "accountID": aid, "at": T, "balances": [{"amount": amt, "currency": "USD"}]})
        self.send(404, {"message": "not found"})
HTTPServer(("127.0.0.1", 9100), H).serve_forever()
