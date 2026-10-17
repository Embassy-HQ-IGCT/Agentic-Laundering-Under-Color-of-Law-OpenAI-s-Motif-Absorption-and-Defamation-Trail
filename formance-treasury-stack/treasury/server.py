#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Treasury engine HTTP API (stdlib).

Security model
  * Every route except /livez and /readyz requires a signed request:  X-Key-Id, X-Timestamp (unix s), X-Signature =
    hex(HMAC-SHA256(secret, "<ts>\\n<METHOD>\\n<path?query>\\n<sha256(body)>")). +/-300 s replay window. Keys carry SCOPES and a rate limit.
  * Rafiki webhooks are authenticated by Rafiki's own signature (Rafiki-Signature: t=<ms>, v1=<hmac>) with RAFIKI_WEBHOOK_SECRET.
  * Stripe Issuing webhooks are authenticated by Stripe-Signature (stripe.WebhookSignature): the synchronous authorization endpoint
    with STRIPE_ISSUING_AUTH_WEBHOOK_SECRET, the event endpoint with STRIPE_WEBHOOK_SECRET.
  * Mutations are idempotent and bound to their request content (same id + different body -> 409).
  * Production mode (default) refuses to start with: no API keys, mock banks, the test clock, or the admin/test endpoints.
Operations
  * /livez, /readyz (ledger + db + not draining) on the API port; Prometheus /metrics on METRICS_PORT (keep it cluster-internal).
  * JSON access logs, graceful SIGTERM drain, and a background recovery loop (resume stuck payouts, resolve unknown outcomes, expire holds).
Env: TREASURY_ENV (prod|test), DATABASE_URL, LEDGER_URL, LEDGER_NAME, TREASURY_API_KEYS (json), RAFIKI_WEBHOOK_SECRET,
     STRIPE_ISSUING_AUTH_WEBHOOK_SECRET, STRIPE_WEBHOOK_SECRET, PORT, METRICS_PORT,
     TREASURY_BG (1|0), TREASURY_BG_INTERVAL_S, MAX_BODY_BYTES, BANKS_JSON, TREASURY_ALLOW_MOCK_BANKS, FX_RATES_JSON.
"""
import datetime as dt
import hashlib
import hmac
import json
import os
import re
import signal
import socket
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import stripe

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rails as railmod
from banks import load_banks
from engine import Declined, NotFound, SimulatedCrash, Treasury, ValidationError
from ledger import IdempotencyConflict, Ledger, LedgerError
from store import Store

ENV = os.environ.get("TREASURY_ENV", "prod")
TEST = ENV == "test"
MAX_BODY = int(os.environ.get("MAX_BODY_BYTES", 65536))
REPLAY_WINDOW_S = 300
T = None                      # Treasury instance (set in main)
KEYS = {}                     # key id -> {"secret","scopes","rps"}
RAFIKI_SECRET = os.environ.get("RAFIKI_WEBHOOK_SECRET", "")
STRIPE_SECRETS = {"STRIPE_AUTH": os.environ.get("STRIPE_ISSUING_AUTH_WEBHOOK_SECRET", ""),
                  "STRIPE": os.environ.get("STRIPE_WEBHOOK_SECRET", "")}
DRAINING = threading.Event()

# ------------------------------------------------------------------------------------------------ metrics
_mlock = threading.Lock()
REQS, LAT, CARD = {}, {}, {}
BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5)


def observe(route, method, status, secs, body=None):
    with _mlock:
        REQS[(route, method, status)] = REQS.get((route, method, status), 0) + 1
        h = LAT.setdefault(route, {"b": [0] * len(BUCKETS), "sum": 0.0, "n": 0})
        h["sum"] += secs
        h["n"] += 1
        for i, b in enumerate(BUCKETS):
            if secs <= b:
                h["b"][i] += 1
        if route in ("/card/authorize", "/stripe/issuing/authorizations") and isinstance(body, dict) and "approved" in body:
            k = (str(body["approved"]).lower(), str(body.get("response_code") or (body.get("metadata") or {}).get("response_code")))
            CARD[k] = CARD.get(k, 0) + 1


def metrics_text():
    out = ["# TYPE treasury_http_requests_total counter"]
    with _mlock:
        for (r, m, s), n in sorted(REQS.items()):
            out.append(f'treasury_http_requests_total{{route="{r}",method="{m}",status="{s}"}} {n}')
        out.append("# TYPE treasury_http_request_seconds histogram")
        for r, h in sorted(LAT.items()):
            for i, b in enumerate(BUCKETS):
                out.append(f'treasury_http_request_seconds_bucket{{route="{r}",le="{b}"}} {h["b"][i]}')
            out.append(f'treasury_http_request_seconds_bucket{{route="{r}",le="+Inf"}} {h["n"]}')
            out.append(f'treasury_http_request_seconds_sum{{route="{r}"}} {h["sum"]:.6f}')
            out.append(f'treasury_http_request_seconds_count{{route="{r}"}} {h["n"]}')
        out.append("# TYPE treasury_card_decisions_total counter")
        for (a, c), n in sorted(CARD.items()):
            out.append(f'treasury_card_decisions_total{{approved="{a}",response_code="{c}"}} {n}')
    out.append("# TYPE treasury_payouts gauge")
    try:
        for r in T.q("select status, count(*) n from payouts group by status"):
            out.append(f'treasury_payouts{{status="{r["status"]}"}} {r["n"]}')
        out.append("# TYPE treasury_payouts_needing_attention gauge")
        out.append(f'treasury_payouts_needing_attention {T.q1("select count(*) n from payouts where status = ?", "review")["n"]}')
    except Exception:
        pass
    out.append("# TYPE treasury_up gauge\ntreasury_up 1")
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------------------------------------ rate limiting
_buckets, _block = {}, threading.Lock()


def allow(key_id, rps):
    now = time.monotonic()
    with _block:
        tokens, last = _buckets.get(key_id, (rps * 2.0, now))
        tokens = min(rps * 2.0, tokens + (now - last) * rps)
        if tokens < 1:
            _buckets[key_id] = (tokens, now)
            return False
        _buckets[key_id] = (tokens - 1, now)
        return True


# ------------------------------------------------------------------------------------------------ auth
def sign(secret, ts, method, target, body):
    msg = f"{ts}\n{method}\n{target}\n{hashlib.sha256(body).hexdigest()}"
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


class AuthError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status, self.msg = status, msg


def authenticate(h, method, target, body, scope):
    kid, ts, sig = h.headers.get("X-Key-Id"), h.headers.get("X-Timestamp"), h.headers.get("X-Signature")
    if not (kid and ts and sig):
        raise AuthError(401, "missing X-Key-Id / X-Timestamp / X-Signature")
    key = KEYS.get(kid)
    try:
        fresh = abs(time.time() - int(ts)) <= REPLAY_WINDOW_S
    except ValueError:
        fresh = False
    expected = sign(key["secret"] if key else "x", ts, method, target, body)
    if not (key and fresh and hmac.compare_digest(expected, sig)):          # constant-time; one error for every failure mode
        raise AuthError(401, "invalid credentials")
    if scope not in key["scopes"] and "*" not in key["scopes"]:
        raise AuthError(403, f"key lacks scope '{scope}'")
    if not allow(kid, key.get("rps", 200)):
        raise AuthError(429, "rate limit exceeded")
    return kid


def canonical_json(o):
    return json.dumps(o, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def verify_rafiki(h, body):
    """Rafiki-Signature: t=<ms>, v1=<hex hmac_sha256(secret, '<t>.<JCS({id,type,data})>')>  (see rafiki backend webhook/service.ts)."""
    if not RAFIKI_SECRET:
        raise AuthError(503, "RAFIKI_WEBHOOK_SECRET not configured")
    hdr = h.headers.get("Rafiki-Signature", "")
    m = re.match(r"^t=(\d+), v1=([0-9a-f]{64})$", hdr)
    if not m:
        raise AuthError(401, "invalid credentials")
    t_ms, digest = int(m.group(1)), m.group(2)
    try:
        ev = json.loads(body)
        payload = f"{t_ms}.{canonical_json({'id': ev['id'], 'type': ev['type'], 'data': ev['data']})}"
    except (ValueError, KeyError, TypeError):
        raise AuthError(400, "malformed webhook")
    expected = hmac.new(RAFIKI_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    if abs(time.time() * 1000 - t_ms) > REPLAY_WINDOW_S * 1000 or not hmac.compare_digest(expected, digest):
        raise AuthError(401, "invalid credentials")
    return "rafiki"


def verify_stripe(h, body, scope):
    secret = STRIPE_SECRETS[scope]
    if not secret:
        raise AuthError(503, "Stripe webhook secret not configured")
    try:
        stripe.WebhookSignature.verify_header(body.decode(), h.headers.get("Stripe-Signature", ""), secret, tolerance=REPLAY_WINDOW_S)
    except (stripe.SignatureVerificationError, UnicodeDecodeError):
        raise AuthError(401, "invalid credentials")
    return "stripe"


# ------------------------------------------------------------------------------------------------ routes
ROUTES = []


def route(method, pattern, scope):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), pattern, scope, fn))
        return fn
    return deco


def now_of(h):
    if TEST and h.headers.get("X-Now"):
        return dt.datetime.fromisoformat(h.headers["X-Now"]).astimezone(dt.timezone.utc)
    return dt.datetime.now(dt.timezone.utc)


@route("GET", "/livez", None)
def _(h, b, m): return {"status": "ok"}

@route("GET", "/readyz", None)
def _(h, b, m):
    r = T.ready()
    ok = all(r.values()) and not DRAINING.is_set()
    return (200 if ok else 503, dict(r, draining=DRAINING.is_set()))

@route("GET", "/rails", "read")
def _(h, b, m): return railmod.matrix(now_of(h))

@route("GET", "/rates", "read")
def _(h, b, m): return {"base": "USD", "rates": dict({"USD": 1.0}, **{k[:3]: round(1 / v, 6) for k, v in T.fx_mid.items() if k.endswith("USD")})}

@route("GET", "/wallets/([^/]+)", "read")
def _(h, b, m): return T.wallet(m[0], h.query.get("ccy", "USD"))

@route("POST", "/transfers", "transfer")
def _(h, b, m): return T.transfer(b)

@route("POST", "/payouts", "payout")
def _(h, b, m): return T.payout(b, now_of(h))

@route("GET", "/payouts/([^/]+)", "read")
def _(h, b, m): return T.get_payout(m[0]) or (404, {"error": "not found"})

@route("POST", "/payouts/poll", "payout:admin")
def _(h, b, m): return {"advanced": T.poll_payouts(now_of(h))}

@route("POST", "/payouts/recover", "payout:admin")
def _(h, b, m): return T.recover(now_of(h), int((b or {}).get("stale_s", 60)), force=TEST and bool((b or {}).get("force")))

@route("POST", "/payouts/([^/]+)/resolve", "payout:admin")
def _(h, b, m): return T.resolve_payout(m[0], now_of(h))

@route("POST", "/cards", "card:admin")
def _(h, b, m): return T.card_issue(b)

@route("POST", "/card/authorize", "card")
def _(h, b, m): return T.card_authorize(b, now_of(h))

@route("POST", "/card/capture", "card")
def _(h, b, m): return T.card_capture(b, now_of(h))

@route("POST", "/card/reverse", "card")
def _(h, b, m): return T.card_reverse(b, now_of(h))

@route("POST", "/card/expire", "card:admin")
def _(h, b, m): return {"expired": T.card_expire(now_of(h))}

@route("POST", "/fx/quote", "fx")
def _(h, b, m): return T.fx_quote(b, now_of(h))

@route("POST", "/fx/convert", "fx")
def _(h, b, m): return T.fx_convert(b, now_of(h))

@route("GET", "/fx/exposure", "read")
def _(h, b, m): return T.fx_exposure()

@route("POST", "/sweeps", "sweep")
def _(h, b, m): return T.sweep(b)

@route("POST", "/liquidity", "read")
def _(h, b, m): return T.liquidity((b or {}).get("targets"))

@route("POST", "/credit/facilities", "credit:admin")
def _(h, b, m): return T.credit_open(b, now_of(h))

@route("GET", "/credit/facilities/([^/]+)", "read")
def _(h, b, m): return T.credit_status(m[0])

@route("POST", "/credit/draw", "credit")
def _(h, b, m): return T.credit_draw(b)

@route("POST", "/credit/repay", "credit")
def _(h, b, m): return T.credit_repay(b)

@route("POST", "/credit/accrue", "credit:admin")
def _(h, b, m): return T.credit_accrue(b, now_of(h))

@route("POST", "/guarantees", "guarantee")
def _(h, b, m): return T.guarantee_issue(b, now_of(h))

@route("POST", "/guarantees/claim", "guarantee")
def _(h, b, m): return T.guarantee_claim(b)

@route("POST", "/guarantees/release", "guarantee")
def _(h, b, m): return T.guarantee_release(b)

@route("POST", "/recon/([^/]+)", "recon")
def _(h, b, m): return T.reconcile(m[0], now_of(h), (b or {}).get("since"))

@route("GET", "/bank/([^/]+)/instructions", "bank:ops")
def _(h, b, m): return T.manual_instructions(m[0], h.query.get("status"))

@route("POST", "/bank/([^/]+)/instructions/([^/]+)/sent", "bank:ops")
def _(h, b, m): return T.manual_sent(m[0], m[1], b)

@route("POST", "/bank/([^/]+)/instructions/([^/]+)/failed", "bank:ops")
def _(h, b, m): return T.manual_failed(m[0], m[1], b)

@route("POST", "/bank/([^/]+)/statement", "bank:ops")      # body: {"format": "csv"|"bai2", "content": "..."}; raise MAX_BODY_BYTES for big files
def _(h, b, m): return T.manual_statement(m[0], now_of(h), b)

@route("POST", "/bank/([^/]+)/credit", "webhook:bank")
def _(h, b, m): return T.apply_inbound(m[0], b)

@route("POST", "/rafiki/webhooks", "RAFIKI")
def _(h, b, m): return T.rafiki_event(b)

@route("POST", "/stripe/issuing/authorizations", "STRIPE_AUTH")
def _(h, b, m):
    if b.get("type") != "issuing_authorization.request":
        return 400, {"error": "expected issuing_authorization.request"}
    return 200, T.stripe_issuing_authorization(b, now_of(h)), {"Stripe-Version": b["api_version"]}

@route("POST", "/stripe/webhooks", "STRIPE")
def _(h, b, m): return T.stripe_issuing_event(b, now_of(h))

if TEST:      # fault injection and bank simulation - never registered in production
    @route("POST", "/admin/post", "admin")
    def _(h, b, m):
        r = T.led.tx(b["ref"], [tuple(p) for p in b["postings"]], metadata=b.get("metadata"))
        return {"ledger_tx": r["id"], "duplicate": r["dup"]}

    @route("POST", "/admin/crash", "admin")
    def _(h, b, m):
        T.crash_at = b.get("point")
        return {"crash_at": T.crash_at}

    @route("POST", "/admin/bank/([^/]+)/inbound", "admin")
    def _(h, b, m): return {"bank_ref": T.banks[m[0]].inject_inbound(b["ccy"], int(b["amount"]), b["narrative"], b.get("rail", "FEDWIRE"), now_of(h))}

    @route("POST", "/admin/bank/([^/]+)/debit", "admin")
    def _(h, b, m): return {"bank_ref": T.banks[m[0]].inject_debit(b["ccy"], int(b["amount"]), b["narrative"], now_of(h))}

    @route("POST", "/admin/bank/([^/]+)/control", "admin")
    def _(h, b, m):
        bank = T.banks[m[0]]
        for k in ("down", "fail_next", "refuse_connect", "timeout_after_send", "timeout_before_send"):
            if k in b:
                setattr(bank, k, b[k])
        return {k: getattr(bank, k) for k in ("down", "fail_next", "refuse_connect", "timeout_after_send", "timeout_before_send")}

    @route("GET", "/admin/bank/([^/]+)/submissions", "admin")
    def _(h, b, m): return dict(T.banks[m[0]].submissions)

    @route("GET", "/admin/balance/([^?]+)", "admin")
    def _(h, b, m): return {"account": m[0], "balance": T.led.balance(m[0], f"{h.query.get('ccy', 'USD')}/2")}

    @route("GET", "/admin/ledger/check/([A-Z]+)", "admin")
    def _(h, b, m):
        total, accts = T.led.all_balances(f"{m[0]}/2")
        return {"sum": total, "accounts": len(accts)}

    @route("GET", "/admin/tx/(.+)", "admin")
    def _(h, b, m): return T.led.tx_by_reference(m[0]) or (404, {"error": "no such reference"})

    @route("GET", "/admin/audit", "admin")
    def _(h, b, m): return {"rows": T.q("select ts, actor, action, ref from audit order by ts desc limit 50")}


# ------------------------------------------------------------------------------------------------ http plumbing
def log(level, **kv):
    print(json.dumps(dict({"ts": dt.datetime.now(dt.timezone.utc).isoformat(), "level": level}, **kv)), flush=True)


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 15                       # idle keep-alive / slow-client cutoff (also lets graceful drain finish)

    def log_message(self, *a): pass

    def setup(self):
        super().setup()
        self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def _send(self, code, body, headers=None):
        raw = json.dumps(body).encode()
        conn = "close" if DRAINING.is_set() else "keep-alive"
        if conn == "close":
            self.close_connection = True
        head = (f"HTTP/1.1 {code} {'OK' if code < 300 else 'ERR'}\r\nContent-Type: application/json\r\nContent-Length: {len(raw)}\r\n"
                f"Connection: {conn}\r\nX-Content-Type-Options: nosniff\r\nCache-Control: no-store\r\n"
                + "".join(f"{k}: {v}\r\n" for k, v in (headers or {}).items()) + "\r\n").encode()
        self.wfile.write(head + raw)
        self.wfile.flush()
        return code

    def _handle(self, method):
        t0, req_id = time.perf_counter(), uuid.uuid4().hex[:12]
        target = self.path
        path, _, qs = target.partition("?")
        self.query = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
        label, status, actor, resp = path, 500, "-", None
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                status = self._send(413, {"error": "request too large"})
                self.close_connection = True
                return
            raw = self.rfile.read(n) if n else b""
            for mth, rx, pattern, scope, fn in ROUTES:
                mt = rx.match(path)
                if mth != method or not mt:
                    continue
                label = pattern
                try:
                    if scope == "RAFIKI":
                        actor = verify_rafiki(self, raw)
                    elif scope in STRIPE_SECRETS:
                        actor = verify_stripe(self, raw, scope)
                    elif scope:
                        actor = authenticate(self, method, target, raw, scope)
                    try:
                        body = json.loads(raw) if raw else {}
                    except ValueError:
                        status = self._send(400, {"error": "invalid JSON"})
                        return
                    if method == "POST" and not isinstance(body, dict):
                        status = self._send(400, {"error": "body must be a JSON object"})
                        return
                    T.ctx.actor = actor
                    T.led.set_context(actor=actor, req=req_id)
                    resp = fn(self, body, mt.groups())
                    status = self._send(*resp) if isinstance(resp, tuple) else self._send(200, resp)
                    resp = resp[1] if isinstance(resp, tuple) else resp
                except AuthError as e:
                    status = self._send(e.status, {"error": e.msg})
                except Declined as e:
                    status = self._send(422, {"declined": True, "reason": e.reason, "response_code": e.code})
                except IdempotencyConflict as e:
                    status = self._send(409, {"error": "idempotency_conflict", "detail": str(e)})
                except NotFound as e:
                    status = self._send(404, {"error": str(e)})
                except (ValidationError, ValueError) as e:
                    status = self._send(400, {"error": str(e)})
                except LedgerError as e:
                    log("error", req=req_id, msg="ledger error", code=e.code)
                    status = self._send(502, {"error": "ledger", "code": e.code})
                except SimulatedCrash:
                    self.close_connection = True      # test-only: model a process dying mid-request (no response)
                    status = 0
                    self.connection.close()
                except Exception:
                    log("error", req=req_id, msg="unhandled", trace=traceback.format_exc()[-600:])
                    status = self._send(500, {"error": "internal"})
                finally:
                    T.led.clear_context()
                return
            status = self._send(404, {"error": "no route"})
        finally:
            dur = time.perf_counter() - t0
            observe(label, method, status, dur, resp)
            log("info", req=req_id, method=method, route=label, status=status, actor=actor, ms=round(dur * 1000, 2))

    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")


class MetricsHandler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        body = metrics_text().encode() if self.path == "/metrics" else b"not found"
        self.send_response(200 if self.path == "/metrics" else 404)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Srv(ThreadingHTTPServer):
    request_queue_size = 512
    daemon_threads = False             # server_close() joins in-flight handlers => graceful drain
    block_on_close = True


# ------------------------------------------------------------------------------------------------ startup
def load_keys():
    raw = os.environ.get("TREASURY_API_KEYS", "")
    keys = json.loads(raw) if raw else {}
    for kid, k in keys.items():
        if len(k.get("secret", "")) < 32:
            raise RuntimeError(f"refusing to start: API key {kid!r} secret must be at least 32 characters")
    return keys


def build():
    banks = load_banks()          # config-only and may refuse (mock banks): check before touching any dependency
    led = Ledger(os.environ.get("LEDGER_URL", "http://127.0.0.1:9000"), os.environ.get("LEDGER_NAME", "treasury"))
    led.ensure()
    led.require_reference_index()
    fx = json.loads(os.environ["FX_RATES_JSON"]) if os.environ.get("FX_RATES_JSON") else None
    store = Store(os.environ.get("DATABASE_URL", ":memory:" if TEST else ""))
    return Treasury(led, banks, store, fx_rates=fx)


def preflight():
    problems = []
    if not TEST:
        for v in ("TREASURY_ADMIN", "TREASURY_TEST_CLOCK"):
            if os.environ.get(v):
                problems.append(f"{v} must not be set in production")
        if not os.environ.get("DATABASE_URL", "").startswith(("postgres://", "postgresql://")):
            problems.append("DATABASE_URL must be a PostgreSQL URL in production (state must survive restarts and be shared across replicas)")
        if not KEYS:
            problems.append("TREASURY_API_KEYS is empty: refusing to serve unauthenticated money movement")
        if not RAFIKI_SECRET:
            log("warn", msg="RAFIKI_WEBHOOK_SECRET unset: /rafiki/webhooks will reject all requests")
        for scope, var in (("STRIPE_AUTH", "STRIPE_ISSUING_AUTH_WEBHOOK_SECRET"), ("STRIPE", "STRIPE_WEBHOOK_SECRET")):
            if not STRIPE_SECRETS[scope]:
                log("warn", msg=f"{var} unset: its Stripe endpoint will reject all requests")
    if problems:
        raise RuntimeError("refusing to start: " + "; ".join(problems))


def background(interval):
    while not DRAINING.wait(interval):
        try:
            now = dt.datetime.now(dt.timezone.utc)
            r = T.recover(now)
            e = T.card_expire(now)
            if r["resumed"] or r["resolved"] or e:
                log("info", msg="recovery", resumed=len(r["resumed"]), resolved=len(r["resolved"]), expired=len(e))
        except Exception:
            log("error", msg="recovery loop failed", trace=traceback.format_exc()[-600:])


def main():
    global T, KEYS
    KEYS = load_keys()
    preflight()
    T = build()
    port, mport = int(os.environ.get("PORT", "8200")), int(os.environ.get("METRICS_PORT", "9100"))
    srv = Srv(("0.0.0.0", port), H)
    threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", mport), MetricsHandler).serve_forever, daemon=True).start()
    if os.environ.get("TREASURY_BG", "1") == "1":
        threading.Thread(target=background, args=(float(os.environ.get("TREASURY_BG_INTERVAL_S", "30")),), daemon=True).start()

    def drain(signum, _):
        log("info", msg="draining", signal=signum)
        DRAINING.set()
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, drain)
    signal.signal(signal.SIGINT, drain)
    log("info", msg="started", env=ENV, port=port, metrics_port=mport, keys=sorted(KEYS), db="postgres" if T.db.pg else "sqlite")
    srv.serve_forever()
    srv.server_close()                 # waits for in-flight requests
    log("info", msg="stopped")


if __name__ == "__main__":
    main()
