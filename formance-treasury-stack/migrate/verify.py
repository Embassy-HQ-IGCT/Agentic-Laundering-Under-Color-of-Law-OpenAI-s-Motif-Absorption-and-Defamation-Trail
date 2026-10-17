#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Source (Ledger v2) vs mirror (Ledger v3) verification. Read-only; native HTTP APIs only.
Compares transactions (id, postings, reference, metadata, timestamp, reverted), account metadata, per-account/asset
input/output/balance, reference uniqueness and per-asset zero-sum. Exit 0 only when every check passes.

Env: V2_URL V2_LEDGER V3_URL V3_LEDGER
     V2_BEARER_FILE / V3_BEARER_FILE   optional: path to a file holding an access token (read, never printed)
     CROSSWALK_FILE                    optional: JSON {"<v2 address>": "<v3 address>"} matching the mirror rewrite rules
     EXPECT_REFERENCES                 optional: comma-separated references that must exist on both sides
Output: JSON report on stdout."""
import json
import os
import sys
import urllib.parse
import urllib.request

V3_KEYS = ("com.formance.spec/state/reverts", "com.formance.spec/state/reverted")


def headers(var):
    p = os.environ.get(var)
    h = {"accept": "application/json"}
    if p:
        with open(p) as f:
            h["authorization"] = "Bearer " + f.read().strip()
    return h


def get(base, path, hdr):
    with urllib.request.urlopen(urllib.request.Request(base.rstrip("/") + path, headers=hdr), timeout=60) as r:
        return json.loads(r.read())


def pages(base, path, hdr, v2):
    sep = "&" if "?" in path else "?"
    cursor = None
    while True:
        p = get(base, path + (f"{sep}cursor={urllib.parse.quote(cursor)}" if cursor else f"{sep}pageSize=100"), hdr)
        c = p["cursor"] if v2 else p
        yield from c.get("data") or []
        cursor = c.get("next") if c.get("hasMore") else None
        if not cursor:
            return


def main():
    v2, l2, v3, l3 = (os.environ[k] for k in ("V2_URL", "V2_LEDGER", "V3_URL", "V3_LEDGER"))
    h2, h3 = headers("V2_BEARER_FILE"), headers("V3_BEARER_FILE")
    xw = json.load(open(os.environ["CROSSWALK_FILE"])) if os.environ.get("CROSSWALK_FILE") else {}
    a = lambda addr: xw.get(addr, addr)
    report, fails = {"source": f"{v2}/v2/{l2}", "target": f"{v3}/v3/{l3}", "checks": {}}, []

    def check(name, ok, detail=None):
        report["checks"][name] = {"pass": bool(ok), **({"detail": detail} if detail and not ok else {})}
        if not ok:
            fails.append(name)

    t2 = {t["id"]: t for t in pages(v2, f"/v2/{l2}/transactions", h2, True)}
    t3 = {t["id"]: t for t in pages(v3, f"/v3/{l3}/transactions", h3, False)}
    report["counts"] = {"source_transactions": len(t2), "target_transactions": len(t3)}
    check("transaction_count", len(t2) == len(t3), {"source": len(t2), "target": len(t3)})
    check("transaction_ids", set(t2) == set(t3),
          {"missing_on_target": sorted(set(t2) - set(t3))[:50], "extra_on_target": sorted(set(t3) - set(t2))[:50]})
    diff = []
    for i in sorted(set(t2) & set(t3)):
        s, d = t2[i], t3[i]
        sp = [(a(p["source"]), a(p["destination"]), int(p["amount"]), p["asset"]) for p in s["postings"]]
        dp = [(p["source"], p["destination"], int(p["amount"]), p["asset"]) for p in d["postings"]]
        sm = {k: v for k, v in (s.get("metadata") or {}).items()}
        dm = {k: v for k, v in (d.get("metadata") or {}).items()}
        for k in V3_KEYS:
            if k in sm:
                dm.setdefault(k, sm[k])
        fields = {"postings": sp == dp, "reference": s.get("reference") == d.get("reference"), "metadata": sm == dm,
                  "timestamp": s.get("timestamp") == d.get("timestamp"), "reverted": bool(s.get("reverted")) == bool(d.get("reverted"))}
        bad = [k for k, v in fields.items() if not v]
        if bad:
            diff.append({"id": i, "fields": bad})
    check("transaction_content", not diff, diff[:50])

    refs = [t.get("reference") for t in t3.values() if t.get("reference")]
    check("target_reference_unique", len(refs) == len(set(refs)), sorted({r for r in refs if refs.count(r) > 1})[:50])
    for r in [x for x in os.environ.get("EXPECT_REFERENCES", "").split(",") if x]:
        s = [t["id"] for t in t2.values() if t.get("reference") == r]
        d = [t["id"] for t in t3.values() if t.get("reference") == r]
        check(f"reference:{r}", len(s) == 1 and s == d, {"source_ids": s, "target_ids": d})

    acc2, md2 = {}, {}
    for x in pages(v2, f"/v2/{l2}/accounts?expand=volumes", h2, True):
        md2[a(x["address"])] = x.get("metadata") or {}
        for asset, v in (x.get("volumes") or {}).items():
            k = (a(x["address"]), asset)
            o = acc2.setdefault(k, {"input": 0, "output": 0, "balance": 0})
            for f in o:
                o[f] += int(v[f])
    acc3, md3 = {}, {}
    for x in pages(v3, f"/v3/{l3}/accounts", h3, False):
        md3[x["address"]] = x.get("metadata") or {}
        for v in x.get("volumes") or []:
            acc3[(x["address"], v["asset"])] = {f: int(v["volumes"][f]) for f in ("input", "output", "balance")}
    vol = [{"account": k[0], "asset": k[1], "source": acc2.get(k), "target": acc3.get(k)}
           for k in sorted(set(acc2) | set(acc3)) if k[0] != "world" and acc2.get(k) != acc3.get(k)]
    check("account_volumes", not vol, vol[:50])
    mdd = [{"account": k, "source": md2.get(k), "target": md3.get(k)} for k in sorted(set(md2) | set(md3))
           if k != "world" and (md2.get(k) or {}) != (md3.get(k) or {})]
    check("account_metadata", not mdd, mdd[:50])

    sums = {}
    for t in t3.values():
        for p in t["postings"]:
            if p["source"] == "world":
                sums[p["asset"]] = sums.get(p["asset"], 0) + int(p["amount"])
            if p["destination"] == "world":
                sums[p["asset"]] = sums.get(p["asset"], 0) - int(p["amount"])
    held = {}
    for (addr, asset), v in acc3.items():
        if addr != "world":
            held[asset] = held.get(asset, 0) + v["balance"]
    check("target_zero_sum", all(held.get(k, 0) == v for k, v in sums.items()) and set(held) <= set(sums) | {k for k, v in held.items() if v == 0},
          {"issued_from_world": sums, "held_by_accounts": held})

    report["result"] = "PASS" if not fails else "FAIL"
    report["failed"] = fails
    print(json.dumps(report, indent=1, sort_keys=True))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
