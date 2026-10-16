# SPDX-License-Identifier: Apache-2.0
"""Signed HTTP client shared by the test suites. Signs every request exactly as a production client must."""
import hashlib
import hmac
import http.client
import json
import os
import socket
import threading
import time

BASE = "127.0.0.1:8200"
KEY_ID = os.environ.get("TEST_KEY_ID", "test")
SECRET = os.environ.get("TEST_KEY_SECRET", "test-secret-0123456789-0123456789-abcdef")
RAFIKI_SECRET = os.environ.get("RAFIKI_WEBHOOK_SECRET", "rafiki-secret-0123456789-0123456789-abcdef")
_tl = threading.local()


def configure(base):
    global BASE
    BASE = base.replace("http://", "")


def sign(secret, ts, method, target, body):
    return hmac.new(secret.encode(), f"{ts}\n{method}\n{target}\n{hashlib.sha256(body).hexdigest()}".encode(), hashlib.sha256).hexdigest()


def canonical(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def rafiki_header(ev, secret=None, t_ms=None):
    t = t_ms if t_ms is not None else int(time.time() * 1000)
    payload = f"{t}.{canonical({'id': ev['id'], 'type': ev['type'], 'data': ev['data']})}"
    return f"t={t}, v1={hmac.new((secret or RAFIKI_SECRET).encode(), payload.encode(), hashlib.sha256).hexdigest()}"


def _conn():
    c = getattr(_tl, "c", None)
    if c is None:
        c = http.client.HTTPConnection(BASE, timeout=30)
        c.connect()
        c.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        _tl.c = c
    return c


def raw_call(method, path, body_bytes=b"", headers=None, retry=True):
    """Unsigned/hand-crafted request (used by the security tests)."""
    for attempt in (0, 1):
        try:
            c = _conn()
            c.request(method, path, body_bytes or None, dict({"Content-Type": "application/json"}, **(headers or {})))
            r = c.getresponse()
            data = r.read()
            return r.status, (json.loads(data) if data else {})
        except (http.client.HTTPException, OSError):
            _tl.c = None
            if attempt or not retry:
                raise


def call(method, path, body=None, now=None, key=None, rafiki=False, extra=None, retry=True):
    raw = json.dumps(body).encode() if body is not None else b""
    h = dict(extra or {})
    if now:
        h["X-Now"] = now
    if rafiki:
        h["Rafiki-Signature"] = rafiki_header(body)
    else:
        kid, secret = key or (KEY_ID, SECRET)
        ts = str(int(time.time()))
        h.update({"X-Key-Id": kid, "X-Timestamp": ts, "X-Signature": sign(secret, ts, method, path, raw)})
    return raw_call(method, path, raw, h, retry=retry)


def metrics(port=None):
    port = port or int(os.environ.get("METRICS_PORT", 9100))
    c = http.client.HTTPConnection(BASE.split(":")[0], port, timeout=10)
    c.request("GET", "/metrics")
    return c.getresponse().read().decode()
