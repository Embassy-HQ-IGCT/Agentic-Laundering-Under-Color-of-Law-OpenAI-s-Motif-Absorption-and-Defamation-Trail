#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Seed a Ledger v2 test source: transactions with references and metadata, two assets, a revert, account and
transaction metadata set/delete. Usage: seed_v2.py <v2 base url> <ledger>"""
import json, sys, urllib.request
B = f"{sys.argv[1].rstrip('/')}/v2/{sys.argv[2]}"
def rq(m, p, body=None):
    r = urllib.request.Request(B + p, method=m, data=json.dumps(body).encode() if body is not None else None, headers={"content-type": "application/json"})
    try:
        with urllib.request.urlopen(r) as x: d = x.read(); return x.status, (json.loads(d) if d else {})
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"{}")
print(rq("POST", "", {}))
tx = lambda postings, ref=None, md=None: rq("POST", "/transactions", {"postings": [{"source": s, "destination": d, "amount": a, "asset": c} for s, d, a, c in postings], **({"reference": ref} if ref else {}), "metadata": md or {}})
ids = []
for i in range(12):
    s, b = tx([("world", "treasury:corpus:main", 10_000_000 + i, "USD/2")], ref=f"SEED-{i}", md={"economic_event": f"INT-seed-{i}"})
    ids.append(b["data"]["id"])
s, b = tx([("treasury:corpus:main", "edge:BOA_PRIVATE_EDGE", 2_500_000, "USD/2"), ("world", "treasury:corpus:eur", 77_700, "EUR/2")],
          ref="52a041a4-d27d-4612-9366-c962d7b69ef8-EXEC2", md={"economic_event": "INT-8711f453519d7d58b456c27b", "state": "PROCESSING"})
ids.append(b["data"]["id"])
s, b = tx([("treasury:corpus:main", "liquidity:wallet:corpus-reflection", 123_456, "USD/2")], ref="REVERT-ME")
rv = rq("POST", f"/transactions/{b['data']['id']}/revert", {}); print("revert", rv[0])
print(rq("POST", "/accounts/treasury:corpus:main/metadata", {"classification": "CORPUS", "situs": "LI"})[0])
print(rq("POST", "/accounts/edge:BOA_PRIVATE_EDGE/metadata", {"tmp": "x"})[0])
print(rq("DELETE", "/accounts/edge:BOA_PRIVATE_EDGE/metadata/tmp")[0])
print(rq("POST", f"/transactions/{ids[0]}/metadata", {"note": "amended"})[0])
print("tx ids", ids)
