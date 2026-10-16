#!/usr/bin/env python3
"""Treasury engine HTTP API (stdlib). Env: LEDGER_URL, LEDGER_NAME, DB_PATH, PORT, BANKS_JSON, FX_RATES_JSON,
TREASURY_TEST_CLOCK=1 (honour X-Now header; tests only), TREASURY_ADMIN=1 (enable /admin/* seed endpoints; tests/bootstrap only)."""
import datetime as dt
import json
import os
import re
import socket
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(__file__))
import rails as railmod
from banks import load_banks
from engine import Declined, Treasury
from ledger import Ledger, LedgerError

TEST_CLOCK = os.environ.get("TREASURY_TEST_CLOCK") == "1"
ADMIN = os.environ.get("TREASURY_ADMIN") == "1"


def build():
    led = Ledger(os.environ.get("LEDGER_URL", "http://127.0.0.1:9000"), os.environ.get("LEDGER_NAME", "treasury"))
    led.ensure()
    fx = json.loads(os.environ["FX_RATES_JSON"]) if os.environ.get("FX_RATES_JSON") else None
    return Treasury(led, load_banks(), os.environ.get("DB_PATH", ":memory:"), fx_rates=fx)


T = None


def now_of(h):
    if TEST_CLOCK and h.headers.get("X-Now"):
        return dt.datetime.fromisoformat(h.headers["X-Now"]).astimezone(dt.timezone.utc)
    return dt.datetime.now(dt.timezone.utc)


ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
        return fn
    return deco


@route("GET", "/health")
def _(h, b, m): return {"status": "ok"}

@route("GET", "/rails")
def _(h, b, m): return railmod.matrix(now_of(h))

@route("GET", "/rates")
def _(h, b, m):   # Rafiki EXCHANGE_RATES_URL contract: {base, rates{CCY: units per 1 base}}
    return {"base": "USD", "rates": dict({"USD": 1.0}, **{k[:3]: round(1 / v, 6) for k, v in T.fx_mid.items() if k.endswith("USD")})}

@route("GET", "/wallets/([^/]+)")
def _(h, b, m): return T.wallet(m[0], h.query.get("ccy", "USD"))

@route("POST", "/transfers")
def _(h, b, m): return T.transfer(b)

@route("POST", "/payouts")
def _(h, b, m): return T.payout(b, now_of(h))

@route("GET", "/payouts/([^/]+)")
def _(h, b, m): return T.get_payout(m[0]) or (404, {"error": "not found"})

@route("POST", "/payouts/poll")
def _(h, b, m): return {"advanced": T.poll_payouts(now_of(h))}

@route("POST", "/cards")
def _(h, b, m): return T.card_issue(b)

@route("POST", "/card/authorize")
def _(h, b, m): return T.card_authorize(b, now_of(h))

@route("POST", "/card/capture")
def _(h, b, m): return T.card_capture(b, now_of(h))

@route("POST", "/card/reverse")
def _(h, b, m): return T.card_reverse(b, now_of(h))

@route("POST", "/card/expire")
def _(h, b, m): return {"expired": T.card_expire(now_of(h))}

@route("POST", "/fx/quote")
def _(h, b, m): return T.fx_quote(b, now_of(h))

@route("POST", "/fx/convert")
def _(h, b, m): return T.fx_convert(b, now_of(h))

@route("GET", "/fx/exposure")
def _(h, b, m): return T.fx_exposure()

@route("POST", "/sweeps")
def _(h, b, m): return T.sweep(b)

@route("POST", "/liquidity")
def _(h, b, m): return T.liquidity((b or {}).get("targets"))

@route("POST", "/credit/facilities")
def _(h, b, m): return T.credit_open(b, now_of(h))

@route("GET", "/credit/facilities/([^/]+)")
def _(h, b, m): return T.credit_status(m[0])

@route("POST", "/credit/draw")
def _(h, b, m): return T.credit_draw(b)

@route("POST", "/credit/repay")
def _(h, b, m): return T.credit_repay(b)

@route("POST", "/credit/accrue")
def _(h, b, m): return T.credit_accrue(b, now_of(h))

@route("POST", "/guarantees")
def _(h, b, m): return T.guarantee_issue(b, now_of(h))

@route("POST", "/guarantees/claim")
def _(h, b, m): return T.guarantee_claim(b)

@route("POST", "/guarantees/release")
def _(h, b, m): return T.guarantee_release(b)

@route("POST", "/recon/([^/]+)")
def _(h, b, m): return T.reconcile(m[0], now_of(h), (b or {}).get("since"))

@route("POST", "/bank/([^/]+)/credit")
def _(h, b, m): return T.apply_inbound(m[0], b)           # real-time credit notification from a bank

@route("POST", "/rafiki/webhooks")
def _(h, b, m): return T.rafiki_event(b)

if ADMIN:
    @route("POST", "/admin/post")
    def _(h, b, m):
        r = T.led.tx(b["ref"], [tuple(p) for p in b["postings"]], metadata=b.get("metadata"))
        return {"ledger_tx": r["id"], "duplicate": r["dup"]}

    @route("POST", "/admin/bank/([^/]+)/inbound")
    def _(h, b, m):
        return {"bank_ref": T.banks[m[0]].inject_inbound(b["ccy"], int(b["amount"]), b["narrative"], b.get("rail", "FEDWIRE"), now_of(h))}

    @route("POST", "/admin/bank/([^/]+)/control")
    def _(h, b, m):
        bank = T.banks[m[0]]
        for k in ("down", "fail_next", "refuse_connect", "timeout_after_send"):
            if k in b:
                setattr(bank, k, b[k])
        return {"down": bank.down, "fail_next": bank.fail_next, "refuse_connect": bank.refuse_connect, "timeout_after_send": bank.timeout_after_send}

    @route("POST", "/admin/bank/([^/]+)/debit")
    def _(h, b, m):
        return {"bank_ref": T.banks[m[0]].inject_debit(b["ccy"], int(b["amount"]), b["narrative"], now_of(h))}

    @route("GET", "/admin/balance/([^?]+)")
    def _(h, b, m):
        ccy = h.query.get("ccy", "USD")
        return {"account": m[0], "balance": T.led.balance(m[0], f"{ccy}/2")}

    @route("GET", "/admin/ledger/check/([A-Z]+)")
    def _(h, b, m):
        total, accts = T.led.all_balances(f"{m[0]}/2")
        return {"sum": total, "accounts": len(accts)}


class Srv(ThreadingHTTPServer):
    request_queue_size = 512      # default backlog (5) resets connections under bursty authorization traffic
    daemon_threads = True


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a): pass

    def setup(self):
        super().setup()
        self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        head = (f"HTTP/1.1 {code} {'OK' if code < 300 else 'ERR'}\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(raw)}\r\n\r\n").encode()
        self.wfile.write(head + raw)            # one write: no Nagle/delayed-ACK stall
        self.wfile.flush()

    def _handle(self, method):
        path, _, qs = self.path.partition("?")
        self.query = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
        n = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(n)) if n else {}
        except ValueError:
            return self._send(400, {"error": "invalid JSON"})
        for mth, rx, fn in ROUTES:
            mt = rx.match(path)
            if mth == method and mt:
                try:
                    res = fn(self, body, mt.groups())
                    if isinstance(res, tuple):
                        return self._send(*res)
                    return self._send(200, res)
                except Declined as e:
                    return self._send(422, {"declined": True, "reason": e.reason, "response_code": e.code})
                except (ValueError, KeyError) as e:
                    return self._send(400, {"error": str(e)})
                except LedgerError as e:
                    return self._send(502, {"error": "ledger", "code": e.code, "detail": e.message})
                except Exception as e:   # never leak a stack trace to callers; log it
                    traceback.print_exc()
                    return self._send(500, {"error": "internal", "detail": type(e).__name__})
        self._send(404, {"error": "no route"})

    def do_GET(self): self._handle("GET")
    def do_POST(self): self._handle("POST")


if __name__ == "__main__":
    T = build()
    port = int(os.environ.get("PORT", "8200"))
    print(f"treasury engine on :{port} (test_clock={TEST_CLOCK} admin={ADMIN})", flush=True)
    Srv(("0.0.0.0", port), H).serve_forever()
