"""Fraud rules = a metric + a threshold + a human-readable statement. Deterministic; no model involved.

Outcome: FAIL (signal), PASS (no signal), CANNOT (data missing, never treated as clear), NA (rule does not apply to this subject).
A FAIL is an *indicator*, not confirmed fraud.
"""
from __future__ import annotations

from decimal import Decimal as D
from typing import Optional

from .metrics import compute
from .thresholds import TH

ALL_SUBJECTS = ("transaction", "merchant", "beneficiary", "payout")
PAYOUT_SUBJECTS = ("beneficiary", "payout")


def _ge(key):
    return lambda v: (D(str(v)) if isinstance(v, str) else v) >= (D(str(TH[key])) if isinstance(TH[key], str) else TH[key])


def _gt_num(key):
    return lambda v: D(str(v)) > D(str(TH[key]))


def _le_num(key):
    return lambda v: D(str(v)) <= D(str(TH[key]))


def _pos(v):
    return D(str(v)) > 0


# id, domain, applies-to, weight, metric, args, test(value)->bool, statement template, next action
RULES = [
    ("amount_outlier", "transaction", ("transaction", "merchant"), 10, "amount_outliers", {}, lambda v: v > 0, "{v} capture(s) with an abnormal amount ({why})", "Review the outlying captures against the merchant's usual order sizes."),
    ("velocity_burst", "transaction", ("transaction", "merchant"), 10, "burst", {}, _ge("burst_min_captures"), "{v} captures within {win} minutes", "Check whether the burst is a known sale or scripted traffic."),
    ("amount_uniform", "transaction", ("transaction", "merchant"), 10, "amount_uniform", {}, _le_num("uniform_max_spread"), "capture sizes differ by {v} (smallest vs largest)", "Check for payments split to stay under a limit."),
    ("repeated_failures", "transaction", ("transaction", "merchant"), 10, "failed_repeats", {}, _ge("failed_repeat_min"), "{v} failed captures by one payer within the window", "Look for card testing or credential stuffing."),
    ("unusual_timing", "transaction", ("transaction", "merchant"), 5, "night_share", {}, _ge("night_share"), "{v} share of captures between 00:00 and 05:59 UTC", "Compare with the merchant's customer time zones."),
    ("fan_in_verified", "network", PAYOUT_SUBJECTS, 25, "linked_payers", {}, _ge("fan_in_min_payers"), "{v} distinct verified payers feed this payout", "Review why this many unrelated payers fund one beneficiary."),
    ("shared_device", "network", PAYOUT_SUBJECTS, 30, "shared_field", {"field": "device"}, _ge("shared_group_min"), "{v} payers share one device", "Check whether the shared devices belong to one operator."),
    ("shared_ip", "network", PAYOUT_SUBJECTS, 15, "shared_field", {"field": "ip"}, _ge("shared_group_min"), "{v} payers share one IP address", "Check whether the shared IPs belong to one operator."),
    ("account_cluster", "network", PAYOUT_SUBJECTS, 15, "cluster", {}, _ge("cluster_min_accounts"), "{v} payers form one device/IP-connected cluster", "Examine the cluster for a common controller."),
    ("payout_concentration", "payout", PAYOUT_SUBJECTS, 20, "payout_concentration", {}, _ge("concentration_share"), "one recipient receives {v} of the batch value", "Confirm the recipient's share is expected."),
    ("payout_frequency", "payout", PAYOUT_SUBJECTS, 15, "payout_frequency", {}, _ge("payout_freq_min"), "{v} payout items to one recipient within the window", "Check for payout splitting or looping."),
    ("duplicate_payouts", "payout", PAYOUT_SUBJECTS, 20, "payout_duplicates", {}, lambda v: v > 0, "{v} duplicate payout pair(s)", "Hold duplicates until the sender confirms intent."),
    ("batch_reconciliation", "payout", ("payout",), 25, "batch_reconciliation", {}, _pos, "batch header differs from its items by {v}", "Reconcile the payout batch before release."),
    ("payout_status_anomaly", "payout", PAYOUT_SUBJECTS, 10, "status_anomaly", {}, lambda v: v > 0, "{v} payout item(s) paid inside a denied batch", "Investigate the inconsistent payout statuses."),
    ("disputes_present", "dispute", ALL_SUBJECTS, 10, "dispute_count", {}, lambda v: v > 0, "{v} dispute(s) against the investigated captures", "Read the disputes before any payout is released."),
    ("dispute_ratio", "dispute", ALL_SUBJECTS, 15, "dispute_ratio", {}, _ge("dispute_ratio"), "dispute ratio {v}", "Compare with the merchant's normal dispute rate."),
    ("dispute_repeat_reason", "dispute", ALL_SUBJECTS, 10, "dispute_repeat_reason", {}, _ge("dispute_repeat_reason_min"), "{v} disputes share the same reason", "Check whether the repeated reason points to one scheme."),
]
RULE = {r[0]: r for r in RULES}
DOMAINS = ("transaction", "network", "payout", "dispute")
RING_CORE = ("fan_in_verified",)          # a payout ring needs fan-in plus shared infrastructure, over verified links
RING_SHARED = ("shared_device", "shared_ip", "account_cluster")


def evaluate(bundle: dict, rule_id: str) -> Optional[dict]:
    rid, domain, applies, weight, metric, args, test, tmpl, action = RULE[rule_id]
    if bundle["subject"]["type"] not in applies:
        return None
    m = compute(bundle, metric, args)
    if m["unknown"]:
        outcome, stmt = "CANNOT", f"cannot assess: {m['why']}"
    else:
        try:
            fail = bool(test(m["value"]))
        except Exception:                      # "n/a" values from a metric that had too little data
            fail = False
        outcome = "FAIL" if fail else "PASS"
        stmt = tmpl.format(v=m["value"], why=m["why"], win=TH["burst_window_min"]) if fail else f"no signal ({m['why'] or m['value']})"
    return {"rule_id": rid, "domain": domain, "weight": weight, "metric": metric, "args": args, "value": m["value"], "outcome": outcome,
            "statement": stmt, "refs": m["refs"], "why": m["why"], "action": action if outcome == "FAIL" else None}


def evaluate_domain(bundle: dict, domain: str) -> list:
    return [f for f in (evaluate(bundle, r[0]) for r in RULES if r[1] == domain) if f]


def assess(findings: list, subject_type: str, has_links: bool = False) -> dict:
    """Risk assessment from *accepted* findings only. Returns score, level, decision and the ring verdict."""
    fails = {f["rule_id"]: f for f in findings if f["outcome"] == "FAIL"}
    cannot = [f for f in findings if f["outcome"] == "CANNOT"]
    score = min(99, sum(f["weight"] for f in fails.values()))
    ring = "NOT_APPLICABLE"
    if subject_type in PAYOUT_SUBJECTS:
        net = {f["rule_id"]: f for f in findings if f["domain"] == "network"}
        if all(f["outcome"] == "CANNOT" and "relationship" in f["why"] for f in net.values()) and net:
            ring = "UNKNOWN"
        elif any(f["rule_id"] in fails for f in net.values()) and all(r in fails for r in RING_CORE) and any(r in fails for r in RING_SHARED):
            ring = "VERIFIED_INDICATORS"
        elif net and any(f["outcome"] == "CANNOT" for f in net.values()):
            ring = "UNKNOWN"
        else:
            ring = "NOT_INDICATED"
    level = "High" if score >= 70 else "Medium" if score >= 40 else "Low"
    unclearable = ring == "UNKNOWN" and has_links      # verified links exist but shared-infrastructure data is missing: cannot be cleared
    if score >= TH["hold_score"] and ring == "VERIFIED_INDICATORS":
        dec = "HOLD"
    elif score >= TH["review_score"] or unclearable:
        dec = "REVIEW"
    else:
        dec = "RELEASE"
    return {"score": score, "level": level, "decision": dec, "ring": ring, "indicators": sorted(fails), "missing": [f["rule_id"] for f in cannot]}
