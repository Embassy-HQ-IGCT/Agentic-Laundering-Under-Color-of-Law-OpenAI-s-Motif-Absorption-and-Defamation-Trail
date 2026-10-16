#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Mock account-servicing-entity endpoints Rafiki calls: /rates (FX) and /webhooks (events). Logs webhooks to webhooks.jsonl."""
import json, os
from http.server import BaseHTTPRequestHandler, HTTPServer
LOG = os.environ.get("WEBHOOK_LOG", "webhooks.jsonl")
RATES = {"base": "USD", "rates": {"EUR": 0.92, "GBP": 0.79, "USD": 1.0}}
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body):
        b = json.dumps(body).encode(); self.send_response(code); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        self._send(200, RATES) if self.path.startswith("/rates") else self._send(404, {})
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); raw = self.rfile.read(n).decode()
        if self.path.startswith("/webhooks"):
            open(LOG, "a").write(raw + "\n"); self._send(200, {})
        else: self._send(404, {})
HTTPServer(("127.0.0.1", 3100), H).serve_forever()
