"""Deterministic metrics over an evidence bundle. Money is Decimal throughout. Each metric returns

    {"value": <str|int>, "unknown": bool, "why": str, "refs": [{"kind","ref"}...]}

``unknown`` is True when the data needed is missing (never defaulted). The Evidence Auditor re-runs these on a freshly
loaded bundle, so a metric is also the verification primitive for claims.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal as D

from .financial import UNKNOWN, is_unknown
from .thresholds import TH

OK_CAPTURE = ("COMPLETED",)
FAILED = ("DENIED", "DECLINED", "FAILED")
BAD_ITEM = ("FAILED", "BLOCKED", "DENIED", "RETURNED", "REFUNDED")


def ref(kind, i):
    return {"kind": kind, "ref": i}


def R(value, unknown=False, why="", refs=None):
    return {"value": value, "unknown": unknown, "why": why, "refs": (refs or [])[:8]}


def completed(b):
    return [c for c in b["captures"] if c["status"] in OK_CAPTURE]


def linked(b):
    ids = {l["capture_id"] for l in b["links"]}
    return [c for c in b["captures"] if c["capture_id"] in ids and c["status"] in OK_CAPTURE]


def dec(x):
    return D(str(x))


# ---------------------------------------------------------------- transaction
def amount_outliers(b, a):
    caps = [c for c in completed(b) if not is_unknown(c["amount"])]
    if len(caps) < int(TH["outlier_min_captures"]):
        return R(0, False, f"only {len(caps)} completed captures; too few for an outlier test")
    cur = defaultdict(list)
    for c in caps:
        cur[c["currency"]].append(c)
    group = max(cur.values(), key=len)
    vals = [dec(c["amount"]) for c in group]
    med = statistics.median(vals)
    mad = statistics.median([abs(v - med) for v in vals])
    if mad == 0:
        return R(0, False, "amounts have zero spread; no outlier test possible")
    z = dec(TH["outlier_mad_z"])
    out = [c for c in group if abs(D("0.6745") * (dec(c["amount"]) - med) / mad) > z]
    return R(len(out), False, f"median {med}, MAD {mad}", [ref("capture", c["capture_id"]) for c in out])


def burst(b, a):
    caps = completed(b)
    if any(c["t"] is None for c in caps):
        return R(0, True, "capture timestamps missing")
    ts = sorted((c["t"], c["capture_id"]) for c in caps)
    w, best, bi = int(TH["burst_window_min"]), 0, (0, 0)
    for i in range(len(ts)):
        j = i
        while j < len(ts) and ts[j][0] - ts[i][0] <= w:
            j += 1
        if j - i > best:
            best, bi = j - i, (i, j)
    return R(best, False, f"max captures within {w} min", [ref("capture", x[1]) for x in ts[bi[0]:bi[1]]])


def amount_uniform(b, a):
    caps = [c for c in completed(b) if not is_unknown(c["amount"])]
    if len(caps) < int(TH["uniform_min_captures"]):
        return R("n/a", False, f"{len(caps)} captures: too few for a size pattern")
    vals = [dec(c["amount"]) for c in caps]
    if max(vals) == 0:
        return R("n/a", False, "all zero")
    return R(str(((max(vals) - min(vals)) / max(vals)).quantize(D("0.0001"))), False, f"{len(vals)} captures, min {min(vals)}, max {max(vals)}", [ref("capture", c["capture_id"]) for c in caps])


def failed_repeats(b, a):
    fails = [c for c in b["captures"] if c["status"] in FAILED]
    if not fails:
        return R(0, False, "no failed captures")
    if any(is_unknown(c["payer_id"]) or c["t"] is None for c in fails):
        return R(0, True, "payer or timestamp unknown for failed captures")
    by = defaultdict(list)
    for c in fails:
        by[c["payer_id"]].append(c)
    best, rf, w = 0, [], int(TH["failed_window_min"])
    for lst in by.values():
        lst.sort(key=lambda c: c["t"])
        for i in range(len(lst)):
            n = [x for x in lst[i:] if x["t"] - lst[i]["t"] <= w]
            if len(n) > best:
                best, rf = len(n), n
    return R(best, False, f"max failed captures by one payer within {w} min", [ref("capture", c["capture_id"]) for c in rf])


def night_share(b, a):
    caps = completed(b)
    if len(caps) < int(TH["night_min_captures"]):
        return R("n/a", False, f"{len(caps)} captures: too few")
    if any(c["t"] is None for c in caps):
        return R("n/a", True, "capture timestamps missing")
    n = sum(1 for c in caps if datetime.fromtimestamp(c["t"] * 60, timezone.utc).hour < 6)
    return R(str((D(n) / D(len(caps))).quantize(D("0.0001"))), False, f"{n} of {len(caps)} captures between 00:00 and 05:59 UTC")


# ---------------------------------------------------------------- network (only over verified links)
def linked_payers(b, a):
    ln = linked(b)
    if not b["links"]:
        return R(0, True, "no verified relationship between captures and this payout (relationship UNKNOWN)")
    known = {c["payer_id"] for c in ln if not is_unknown(c["payer_id"])}
    unk = sum(1 for c in ln if is_unknown(c["payer_id"]))
    return R(len(known), unk > 0 and len(known) < int(TH["fan_in_min_payers"]), f"{len(known)} known payers, {unk} captures with unknown payer", [ref("capture", c["capture_id"]) for c in ln])


def shared_field(b, a):
    f = a["field"]
    ln = linked(b)
    if not b["links"]:
        return R(0, True, "no verified relationship (UNKNOWN)")
    if any(is_unknown(c[f]) or is_unknown(c["payer_id"]) for c in ln):
        return R(0, True, f"{f} or payer missing for some linked captures (PayPal webhooks carry no {f}; it must come from merchant session data)")
    by = defaultdict(set)
    for c in ln:
        by[c[f]].add(c["payer_id"])
    best = max((len(v) for v in by.values()), default=0)
    key = next((k for k, v in by.items() if len(v) == best), None)
    return R(best, False, f"max distinct payers sharing one {f}", [ref("capture", c["capture_id"]) for c in ln if c[f] == key])


def cluster(b, a):
    ln = linked(b)
    if not b["links"]:
        return R(0, True, "no verified relationship (UNKNOWN)")
    if any(is_unknown(c["payer_id"]) for c in ln):
        return R(0, True, "payer unknown for some linked captures")
    if any(is_unknown(c["device"]) and is_unknown(c["ip"]) for c in ln):
        return R(0, True, "device and ip both missing for some linked captures")
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for c in ln:
        find(c["payer_id"])
        for f in ("device", "ip"):
            if not is_unknown(c[f]):
                parent[find(c["payer_id"])] = find(f"{f}:{c[f]}")
    comp = defaultdict(set)
    for c in ln:
        comp[find(c["payer_id"])].add(c["payer_id"])
    best = max((len(v) for v in comp.values()), default=0)
    return R(best, False, "largest group of payers connected by a shared device or IP")


# ---------------------------------------------------------------- payout
def _batch_total(b):
    tot, known = D(0), True
    for bt in b["payout_batches"]:
        if is_unknown(bt["header_amount"]):
            known = False
        else:
            tot += dec(bt["header_amount"])
    return tot, known and bool(b["payout_batches"])


def payout_concentration(b, a):
    items = [i for i in b["payout_items"] if not is_unknown(i["amount"]) and not is_unknown(i["recipient_id"])]
    if len(items) < 1:
        return R("n/a", True, "no payout items with known amount and recipient")
    total, ok = _batch_total(b)
    if not ok:
        total = sum((dec(i["amount"]) for i in items), D(0))
        if b["subject"]["type"] != "payout":
            return R("n/a", True, "batch totals unknown; cannot compute the recipient's share")
    if total == 0:
        return R("n/a", True, "batch total is zero")
    by = defaultdict(D)
    for i in items:
        by[i["recipient_id"]] += dec(i["amount"])
    top, amt = max(by.items(), key=lambda kv: kv[1])
    n_items = len(b["payout_items"]) if b["subject"]["type"] == "payout" else int(b["meta"].get("batch_item_count", 0) or 0)
    if n_items and n_items < int(TH["concentration_min_items"]):
        return R("n/a", False, f"only {n_items} items in the batch; too few for concentration")
    return R(str((amt / total).quantize(D("0.0001"))), False, f"recipient {top} receives {amt} of {total}", [ref("payout_item", i["item_id"]) for i in items if i["recipient_id"] == top])


def payout_frequency(b, a):
    items = [i for i in b["payout_items"] if not is_unknown(i["recipient_id"])]
    if any(i["t"] is None for i in items):
        return R(0, True, "payout timestamps missing")
    by, best, rf, w = defaultdict(list), 0, [], int(TH["payout_freq_window_min"])
    for i in items:
        by[i["recipient_id"]].append(i)
    for lst in by.values():
        lst.sort(key=lambda x: x["t"])
        for k in range(len(lst)):
            n = [x for x in lst[k:] if x["t"] - lst[k]["t"] <= w]
            if len(n) > best:
                best, rf = len(n), n
    return R(best, False, f"max payout items to one recipient within {w} min", [ref("payout_item", i["item_id"]) for i in rf])


def payout_duplicates(b, a):
    items = [i for i in b["payout_items"] if not is_unknown(i["recipient_id"]) and not is_unknown(i["amount"])]
    pairs, rf, w = 0, [], int(TH["duplicate_window_min"])
    for x in range(len(items)):
        for y in range(x + 1, len(items)):
            p, q = items[x], items[y]
            if p["recipient_id"] == q["recipient_id"] and p["amount"] == q["amount"] and p["currency"] == q["currency"]:
                same_key = not is_unknown(p["sender_item_id"]) and p["sender_item_id"] == q["sender_item_id"]
                close = p["t"] is not None and q["t"] is not None and abs(p["t"] - q["t"]) <= w
                if same_key or close:
                    pairs += 1
                    rf += [ref("payout_item", p["item_id"]), ref("payout_item", q["item_id"])]
    return R(pairs, False, "pairs of payout items with the same recipient, amount and currency that share a sender_item_id or occur within the window", rf)


def batch_reconciliation(b, a):
    if not b["payout_batches"]:
        return R("n/a", True, "no payout batch header available")
    if b["subject"]["type"] != "payout":
        return R("n/a", True, "batch items are incomplete for a beneficiary-level view; reconcile from a payout-level investigation")
    diff, why = D(0), []
    for bt in b["payout_batches"]:
        its = [i for i in b["payout_items"] if i["batch_id"] == bt["batch_id"]]
        if is_unknown(bt["header_amount"]) or any(is_unknown(i["amount"]) for i in its):
            return R("n/a", True, f"batch {bt['batch_id']} header or item amount unknown")
        d = abs(dec(bt["header_amount"]) - sum((dec(i["amount"]) for i in its), D(0)))
        if any(i["currency"] != bt["currency"] for i in its):
            d += D("0.01")
            why.append("currency mismatch")
        diff += d
    return R(str(diff), False, "absolute difference between batch headers and the sum of their items" + (f" ({', '.join(why)})" if why else ""), [ref("payout_batch", x["batch_id"]) for x in b["payout_batches"]])


def status_anomaly(b, a):
    bad = 0
    refs = []
    for bt in b["payout_batches"]:
        for i in (x for x in b["payout_items"] if x["batch_id"] == bt["batch_id"]):
            if bt["status"] == "DENIED" and i["status"] == "SUCCESS":
                bad += 1
                refs.append(ref("payout_item", i["item_id"]))
    return R(bad, False, "payout items paid inside a DENIED batch", refs)


# ---------------------------------------------------------------- disputes
def _disputes(b):
    ids = {c["capture_id"] for c in (linked(b) if b["subject"]["type"] in ("beneficiary", "payout") else b["captures"])}
    return [d for d in b["disputes"] if d["capture_id"] in ids]


def dispute_count(b, a):
    ds = _disputes(b)
    return R(len(ds), False, "disputes against the investigated captures", [ref("dispute", d["dispute_id"]) for d in ds])


def dispute_ratio(b, a):
    caps = linked(b) if b["subject"]["type"] in ("beneficiary", "payout") else completed(b)
    if len(caps) < int(TH["dispute_ratio_min_captures"]):
        return R("n/a", False, f"{len(caps)} captures: too few for a ratio")
    return R(str((D(len(_disputes(b))) / D(len(caps))).quantize(D("0.0001"))), False, f"{len(_disputes(b))} disputes over {len(caps)} captures")


def dispute_repeat_reason(b, a):
    ds = [d for d in _disputes(b) if not is_unknown(d["reason"])]
    cnt = defaultdict(list)
    for d in ds:
        cnt[d["reason"]].append(d)
    if not cnt:
        return R(0, False, "no disputes with a known reason")
    k = max(cnt, key=lambda r: len(cnt[r]))
    return R(len(cnt[k]), False, f"most common dispute reason '{k}'", [ref("dispute", d["dispute_id"]) for d in cnt[k]])


METRICS = {f.__name__: f for f in (amount_outliers, burst, amount_uniform, failed_repeats, night_share, linked_payers, shared_field, cluster,
                                   payout_concentration, payout_frequency, payout_duplicates, batch_reconciliation, status_anomaly,
                                   dispute_count, dispute_ratio, dispute_repeat_reason)}


def compute(bundle, name, args=None):
    return METRICS[name](bundle, args or {})
