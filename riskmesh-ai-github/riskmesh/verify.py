"""Independent recomputation used by the reviewer agents.

``recompute_figure`` deliberately does NOT reuse riskmesh.metrics: it is a separate, plain Decimal implementation so a bug
in a specialist's calculation is not silently reproduced by its verifier.
"""
from __future__ import annotations

from decimal import Decimal as D
from typing import Optional

from .financial import is_unknown

KINDS = {"capture": ("captures", "capture_id"), "payout_item": ("payout_items", "item_id"), "payout_batch": ("payout_batches", "batch_id"),
         "dispute": ("disputes", "dispute_id"), "link": ("links", "capture_id")}


def resolve_refs(bundle: dict, evidence: list) -> list:
    """Return the evidence entries that do not point at a real record in the bundle."""
    missing = []
    for e in evidence:
        spec = KINDS.get(e.get("kind"))
        if not spec or e.get("ref") not in {r[spec[1]] for r in bundle.get(spec[0], [])}:
            missing.append(e)
    return missing


def _scope(b: dict, name: str):
    caps, items = b["captures"], b["payout_items"]
    linked = {l["capture_id"] for l in b["links"]}
    if name == "completed_captures":
        return [c for c in caps if c["status"] == "COMPLETED"], "capture"
    if name == "linked_captures":
        return [c for c in caps if c["status"] == "COMPLETED" and c["capture_id"] in linked], "capture"
    if name == "payout_items":
        return list(items), "item"
    if name.startswith("batch_items:"):
        return [i for i in items if i["batch_id"] == name.split(":", 1)[1]], "item"
    if name == "disputes":
        return list(b["disputes"]), "dispute"
    if name == "batch_headers":
        return list(b["payout_batches"]), "batch"
    raise ValueError(f"unknown scope {name}")


def _amount(r, kind):
    return r["header_amount"] if kind == "batch" else r["amount"]


def recompute_figure(b: dict, a: dict) -> tuple:
    """Return (value_as_str | None, note). None means the figure cannot be recomputed (and must not be presented)."""
    fig = a["figure"]
    rows, kind = _scope(b, a["scope"])
    if fig == "count":
        return str(len(rows)), ""
    if fig == "sum_amount":
        cur = {r["currency"] for r in rows}
        if len(cur) > 1:
            return None, f"mixed currencies {sorted(cur)} cannot be summed"
        if any(is_unknown(_amount(r, kind)) for r in rows):
            return None, "some amounts are UNKNOWN"
        if cur and a.get("currency") and a["currency"] not in cur:
            return None, f"currency {sorted(cur)} does not match claimed {a['currency']}"
        return str(sum((D(str(_amount(r, kind))) for r in rows), D(0))), ""
    if fig == "ratio":
        num, _ = _scope(b, a["num"])
        den, _ = _scope(b, a["den"])
        if not den:
            return None, "denominator is zero"
        return str((D(len(num)) / D(len(den))).quantize(D("0.0001"))), ""
    if fig == "distinct_payers":
        rows = [r for r in rows if not is_unknown(r["payer_id"])]
        return str(len({r["payer_id"] for r in rows})), ""
    raise ValueError(f"unknown figure {fig}")


def same_number(x, y) -> bool:
    try:
        return D(str(x)) == D(str(y))
    except Exception:
        return str(x) == str(y)
