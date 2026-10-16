"""Payment rail catalog and availability calendar (America/New_York).

THE WINDOWS BELOW ARE DEFAULTS FOR ROUTING DECISIONS, NOT A SOURCE OF TRUTH. Operators (Fed, TCH) and each bank publish their own
cutoffs and holidays; override via env TREASURY_RAILS_JSON / TREASURY_HOLIDAYS and confirm with the bank before go-live.
What the logic encodes:
  FEDNOW, RTP : 24x7x365, credit-push, irrevocable, seconds. Per-transaction limits are bank/operator set (config).
  FEDWIRE     : business days only. Opens 21:00 ET the evening before a business day, closes 19:00 ET that day.
  ACH         : business days only, discrete submission windows (same-day cutoffs), settles at the next window.
  SWIFT       : cross-border / non-USD; instructions accepted any time, value-dated the next business day (not instant).
"""
import datetime as dt
import json
import os
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

DEFAULT_HOLIDAYS = [  # Federal Reserve holidays that do not need observed-date logic (verify yearly / set TREASURY_HOLIDAYS)
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-05-25", "2026-06-19", "2026-09-07", "2026-10-12", "2026-11-11",
    "2026-11-26", "2026-12-25",
]

DEFAULT_RAILS = {
    "FEDNOW": {"instant": True, "ccy": ["USD"], "max_minor": 100_000_000, "fee_minor": 5, "domestic": True},
    "RTP": {"instant": True, "ccy": ["USD"], "max_minor": 100_000_000, "fee_minor": 4, "domestic": True},
    "FEDWIRE": {"instant": False, "ccy": ["USD"], "max_minor": 10_000_000_000, "fee_minor": 2500, "domestic": True},
    "ACH": {"instant": False, "ccy": ["USD"], "max_minor": 10_000_000_000, "fee_minor": 25, "domestic": True,
            "cutoffs_et": ["10:30", "13:30", "15:45"]},
    "SWIFT": {"instant": False, "ccy": ["*"], "max_minor": 10_000_000_000, "fee_minor": 4500, "domestic": False},
}


def load_rails():
    rails = json.loads(json.dumps(DEFAULT_RAILS))
    for k, v in json.loads(os.environ.get("TREASURY_RAILS_JSON", "{}")).items():
        rails.setdefault(k, {}).update(v)
    return rails


def holidays():
    raw = os.environ.get("TREASURY_HOLIDAYS")
    return {dt.date.fromisoformat(x) for x in (raw.split(",") if raw else DEFAULT_HOLIDAYS)}


def to_et(now):
    return now.astimezone(ET)


def is_business_day(d, hol):
    return d.weekday() < 5 and d not in hol


def _fedwire_open(t, hol):
    today, tomorrow = t.date(), t.date() + dt.timedelta(days=1)
    if is_business_day(today, hol) and t.time() < dt.time(19, 0):
        return True
    return t.time() >= dt.time(21, 0) and is_business_day(tomorrow, hol)


def _ach_state(t, hol, cutoffs):
    """(accepting_same_day, next_window_et). Submissions after the last cutoff or on non-business days go to the next business day."""
    cut = [dt.time.fromisoformat(c) for c in cutoffs]
    if is_business_day(t.date(), hol):
        for c in cut:
            if t.time() < c:
                return True, dt.datetime.combine(t.date(), c, ET)
    d = t.date() + dt.timedelta(days=1)
    while not is_business_day(d, hol):
        d += dt.timedelta(days=1)
    return False, dt.datetime.combine(d, cut[0], ET)


def availability(rail, now, rails=None, hol=None):
    """-> {"open": bool, "instant": bool, "eta": datetime|None, "note": str}. `eta` is when value can be expected."""
    rails = rails or load_rails()
    hol = hol if hol is not None else holidays()
    t, spec = to_et(now), rails[rail]
    if rail in ("FEDNOW", "RTP"):
        return {"open": True, "instant": True, "eta": now, "note": "24x7x365"}
    if rail == "FEDWIRE":
        if _fedwire_open(t, hol):
            return {"open": True, "instant": False, "eta": now, "note": "open"}
        nxt = t
        for _ in range(60 * 24 * 10):
            nxt += dt.timedelta(minutes=15)
            if _fedwire_open(nxt, hol):
                break
        return {"open": False, "instant": False, "eta": nxt.astimezone(dt.timezone.utc), "note": "closed until next open"}
    if rail == "ACH":
        same_day, window = _ach_state(t, hol, spec["cutoffs_et"])
        return {"open": True, "instant": False, "eta": window.astimezone(dt.timezone.utc),
                "note": "same-day window" if same_day else "queued for next business day window"}
    if rail == "SWIFT":   # instructions are accepted at any time; value date is the next business day
        d = t.date() + dt.timedelta(days=1)
        while not is_business_day(d, hol):
            d += dt.timedelta(days=1)
        return {"open": True, "instant": False, "eta": dt.datetime.combine(d, dt.time(9, 0), ET).astimezone(dt.timezone.utc),
                "note": "accepted; value-dated next business day"}
    raise KeyError(rail)


def matrix(now):
    rails, hol = load_rails(), holidays()
    out = {}
    for r in rails:
        a = availability(r, now, rails, hol)
        out[r] = {"open": a["open"], "instant": a["instant"], "eta": a["eta"].isoformat() if a["eta"] else None, "note": a["note"]}
    return out
